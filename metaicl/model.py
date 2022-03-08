# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import numpy as np
import os
import torch
import torch.nn.functional as F
import json

import wandb
from tqdm import tqdm
from transformers import Adafactor, AdamW, get_linear_schedule_with_warmup
from transformers import AutoModelForCausalLM

import test
from utils.utils import get_checkpoint_id, download_file

class MetaICLModel(object):

    def __init__(self, logger=None, out_dir=None, fp16=True, local_rank=-1, model_id="", task=None, debug_data_order=False, model_type=None):
        if logger is None:
            class Logger():
                def info(self, text):
                    print ("Logging from MetaICLModel:\t", text)
            logger = Logger()

        self.logger = logger
        self.out_dir = out_dir
        self.fp16 = fp16
        self.local_rank = local_rank
        self.model_id = model_id
        self.task = task
        self.debug_data_order = debug_data_order
        self.model_type = model_type

        if self.local_rank == -1:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            n_gpu = torch.cuda.device_count()
            ws = 1
        else:  # distributed mode
            torch.cuda.set_device(local_rank)
            device = torch.device("cuda", local_rank)
            ws = int(os.environ.get("WORLD_SIZE", os.environ.get("SLURM_NTASKS", 1)))
            torch.distributed.init_process_group(backend="nccl")
            n_gpu = 1

        self.n_gpu = n_gpu
        self.device = device
        if self.local_rank <= 0:
            logger.info("Setting up for local_rank=%d, world_size=%d" % (self.local_rank, ws))
        self.model_name = None
        self.model = None
        self.mode = None
        self.global_step = None
        self.best_task_dev_score_logfile = os.path.join(self.out_dir, f"{self.model_type}-best_task_dev_score.json")

    def __str__(self):
        text = "[MetaICL Model]: "
        if self.model_name is None:
            text += "No model loaded yet"
        else:
            text += self.model_name
            if self.mode is None:
                text += " (no mode setted - try .train() or .eval()"
            else:
                text += " (%s mode)" % self.mode
        text += "\nusing device %s, %d gpus, local_rank=%d" % (self.device, self.n_gpu, self.local_rank)
        return ("="*50) + "\n" + text + "\n" + ("="*50)

    def is_none(self):
        return self.model is None

    def train(self):
        self.model.train()
        self.mode = "train"

    def eval(self):
        self.model.eval()
        self.mode = "eval"

    def cuda(self):
        self.model.cuda()

    def to_device(self):
        self.model.to(self.device)

    def load(self, checkpoint=None, gpt2="gpt2-large"):
        '''
        checkpoint can be either keyword of the model or path to the checkpoint file
        '''
        if checkpoint is not None and checkpoint.startswith("gpt"):
            gpt2 = checkpoint
            checkpoint = None
        if checkpoint is None:
            if gpt2.startswith("gpt2"):
                model = AutoModelForCausalLM.from_pretrained(gpt2)
            elif gpt2=="gpt-j-6B":
                model = AutoModelForCausalLM.from_pretrained("/checkpoint/sewonmin/gpt-j")
            else:
                raise NotImplementedError(checkpoint)
            self.model_name = gpt2
        else:
            self.model_name = checkpoint
            if checkpoint.endswith('best_task_dev_score.pt'):
                with open(self.best_task_dev_score_logfile, 'r') as f:
                    self.global_step = json.load(f)['global_step']
                    self.logger.info("Reusing checkpoint at %s" % checkpoint)
            else:
                _id = get_checkpoint_id(checkpoint)
                if _id is not None:
                    method, setting, _id = _id
                    keyword = checkpoint
                    checkpoint = os.path.join("checkpoints", method, setting)
                    if self.local_rank <= 0:
                        if not os.path.exists(checkpoint):
                            self.logger.info("Downloading %s in %s", keyword, checkpoint)
                            download_file(_id, checkpoint)

            assert os.path.exists(checkpoint), checkpoint
            self.logger.info("Reusing checkpoint at %s" % checkpoint)
            if self.local_rank <= 0:
                self.logger.info("Loading the model from %s" % checkpoint)
            state_dict = torch.load(checkpoint)
            model = AutoModelForCausalLM.from_pretrained(gpt2, state_dict=state_dict)
        self.model = model


    def save(self, step):
        if self.local_rank <= 0:
            model_state_dict = {key[7:] if key.startswith("module.") else key: value.cpu()
                                for key, value in self.model.state_dict().items()}
            if step == 'best_task_dev_score':
                torch.save(model_state_dict, os.path.join(self.out_dir, f"{self.model_type}-best_task_dev_score.pt"))
            else:
                torch.save(model_state_dict, os.path.join(self.out_dir, f"model{self.model_id}-{step}.pt"))
            self.logger.info(f"Saving model parameters at step={step}")

    def setup_optimizer(self, optimization, num_training_steps, lr, weight_decay, warmup_steps):
        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
                {'params': [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': weight_decay},
                {'params': [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        if optimization=="adafactor":
            optimizer = Adafactor(optimizer_grouped_parameters,
                                  lr=lr,
                                  relative_step=False,
                                  warmup_init=False,
                                  weight_decay=weight_decay)
            scheduler = None
        elif optimization.startswith("adamw"):
            optimizer = AdamW(optimizer_grouped_parameters,
                              lr=lr,
                              eps=1e-08,
                              weight_decay=weight_decay)
            if self.fp16:
                self.model, optimizer = setup_fp16(self.model, optimizer)
            if optimization=="adamw":
                scheduler = get_linear_schedule_with_warmup(optimizer,
                                                            num_warmup_steps=warmup_steps,
                                                            num_training_steps=num_training_steps)
            else:
                raise NotImplementedError()
        elif optimization=="8bit-adam":
            import bitsandbytes as bnb
            optimizer = bnb.optim.Adam8bit(optimizer_grouped_parameters,
                                           lr=lr, betas=(0.9, 0.995))
            if self.fp16:
                self.model, optimizer = setup_fp16(self.model, optimizer)
            scheduler = get_linear_schedule_with_warmup(optimizer,
                                                        num_warmup_steps=warmup_steps,
                                                        num_training_steps=num_training_steps)
        else:
            raise NotImplementedError()

        self.optimizer = optimizer
        self.scheduler = scheduler

    def parallel(self):
        if self.n_gpu > 1:
            self.model = torch.nn.DataParallel(self.model)

        if self.local_rank != -1:
            self.model = torch.nn.parallel.DistributedDataParallel(
                self.model, device_ids=[self.local_rank], output_device=self.local_rank)

    def evaluate_dev_score(self):
        self.save('tmp')
        parser = argparse.ArgumentParser()
        parser.add_argument("--do_zeroshot", default=False, action="store_true")
        parser.add_argument("--use_demonstrations", default=False, action="store_true")
        parser.add_argument("--use_calibration", default=False, action="store_true")
        parser.add_argument("--unseen_domain_only", default=False, action="store_true")

        parser.add_argument("--log_file", default=None, type=str)

        parser.add_argument("--task", type=str, default="SST-2")
        parser.add_argument("--k", type=int, default=16)
        parser.add_argument("--seed", type=str, default="100")

        parser.add_argument("--max_examples_per_task", type=int, default=None)
        parser.add_argument("--test_batch_size", type=int, default=64)
        parser.add_argument("--global_step", type=str, default=None)
        parser.add_argument("--checkpoint", type=str, default=None)

        parser.add_argument("--out_dir", type=str, required=True)

        parser.add_argument("--split", type=str, default=None)
        parser.add_argument("--is_null", default=False, action="store_true")
        parser.add_argument("--method", type=str, default="direct", choices=["direct", "channel"])
        parser.add_argument("--gpt2", type=str, default="gpt2-large")
        args = parser.parse_args(["--out_dir", "/tmp"])

        args.task = self.task
        args.k = 16
        args.split = 'test'
        args.seed = '100,13,21,42,87'
        args.max_examples_per_task = 32
        args.use_demonstrations = True
        args.test_batch_size = 16
        args.method = 'direct'
        args.checkpoint = f'{self.out_dir}/model{self.model_id}-tmp.pt'
        args.out_dir = f'{self.out_dir}/{self.model_id}'
        print(args)
        wandb.config.update({
            'test_args': vars(args) # vars() converts the argparse.Namespace to Dict
        })

        results_dict = test.main(self.logger, args, self)
        return results_dict

    def evaluate_validation_loss(self, val_loader):
        self.model.eval()
        with torch.no_grad():
            val_losses = []
            for batch_idx, vbatch in enumerate(val_loader):

                input_ids=vbatch[0].to(self.device)
                attention_mask=vbatch[1].to(self.device)
                token_type_ids=vbatch[2].to(self.device)
                if len(vbatch)==3:
                    labels=None
                else:
                    labels=vbatch[3].to(self.device)

                val_loss = self.run_model(input_ids, attention_mask, token_type_ids, labels=labels)
                val_losses.append(val_loss.item())
            val_loss = np.mean(val_losses)
            self.logger.info("val_loss %.2f" % (val_loss))
        self.model.train()
        return val_loss

    def do_train(self, data, batch_size, num_training_steps, save_period, log_period,
                 gradient_accumulation_steps=1, max_grad_norm=1.0, val_split=None, label_smoothing=0.0):
        if val_split is not None:
            dataloader, val_loader = data.get_dataloader(batch_size, is_training=True, val_split=val_split)
            self.logger.info(f"len(dataloader) {len(dataloader)}")
            self.logger.info(f"len(val_loader) {len(val_loader)}")
        else:
            dataloader = data.get_dataloader(batch_size, is_training=True)
            self.logger.info(f"len(dataloader) {len(dataloader)}")
        wandb.config.update({
            'dataset_size': len(data),
            'train_dataloader_size': len(dataloader),
            'val_dataloader_size': len(val_loader),
        })
        n_trainable_params = len([param for param in self.model.parameters() if param.requires_grad])
        n_gpus = torch.cuda.device_count()
        self.logger.info("Training {} parameters on {} examples for {} steps using {} GPUs".format(
            n_trainable_params, len(data), num_training_steps, self.n_gpu))

        global_step = 0 if self.global_step is None else self.global_step
        initial_step = global_step # Important for resuming from checkpoints
        stop_training=False
        train_losses = []
        best_accuracy = -1
        val_loss = 0
        best_val_loss = np.inf
        best_dev_score = -np.inf

        while True: 
            for batch_idx, batch in enumerate(dataloader):

                # Evaluate before we train on the batch
                if global_step % log_period == 0:
                    # Validation loss
                    if val_split is not None:
                        self.logger.info("computing val loss")
                        val_loss = self.evaluate_validation_loss(val_loader)
                        self.logger.info(val_loss)
                        if val_loss < best_val_loss:
                            best_val_loss = val_loss
                            # self.save(f"best_val_loss")
                            wandb.run.summary["best_val_loss"] = best_val_loss
                            wandb.run.summary["best_val_loss_global_step"] = global_step
                    
                    # Dev score
                    self.logger.info("computing dev score")
                    dev_results_dict = self.evaluate_dev_score()
                    dev_score = dev_results_dict['mean']
                    self.logger.info(dev_score)
                    if dev_score > best_dev_score:
                        best_dev_score = dev_score
                        # self.save(f"best_dev_score")
                        wandb.run.summary["best_dev_score"] = dev_score
                        wandb.run.summary["best_dev_score_global_step"] = global_step

                        # Additionally keep track of the best dev score across any runs for this task
                        if os.path.exists(self.best_task_dev_score_logfile):
                            with open(self.best_task_dev_score_logfile, 'r') as f:
                                best_task_dev_score = json.load(f)['score']
                        else:
                            best_task_dev_score = 0
                        if dev_score > best_task_dev_score:
                            with open(self.best_task_dev_score_logfile, 'w') as f:
                                json.dump({'score': best_task_dev_score, 'global_step': global_step, 'model_id': self.model_id}, f)
                            wandb.run.summary["best_task_dev_score"] = dev_score
                            wandb.run.summary["best_task_dev_score_global_step"] = global_step
                            self.save(f"best_task_dev_score")


                # Run model through train batch
                input_ids=batch[0].to(self.device)
                attention_mask=batch[1].to(self.device)
                token_type_ids=batch[2].to(self.device)
                if self.debug_data_order:
                    data.print_batch(batch, batch_idx)

                if len(batch)==3:
                    labels=None
                else:
                    labels=batch[3].to(self.device)
                loss = self.run_model(input_ids, attention_mask, token_type_ids, labels=labels, label_smoothing=label_smoothing)
                loss = loss.mean()
                if torch.isnan(loss).data:
                    print ("Stop training because loss=%s" % (loss.data))
                    stop_training=True
                    break
                train_losses.append(loss.detach().cpu())

                # Logging
                if global_step % log_period == 0:
                    train_loss = np.mean(train_losses)
                    train_losses = []
                    self.logger.info("local rank %d\tglobal step %d\ttrain loss %.2f" % (self.local_rank, global_step, train_loss))
                    wandb.log({
                        "global_step": global_step,
                        "epoch": global_step / float(len(dataloader)),
                        "train/loss": train_loss,
                        "val/loss": val_loss,
                        "dev": dev_results_dict,
                        "dev/score": dev_score,
                    })
                # if global_step % save_period == 0:
                #     self.save(global_step)

                # Backprop
                if self.fp16:
                    from apex import amp
                    with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()
                if (global_step + 1) % gradient_accumulation_steps == 0:
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_grad_norm)
                    self.optimizer.step()    # We have accumulated enought gradients
                    if self.scheduler is not None:
                        self.scheduler.step()
                    self.model.zero_grad()

                global_step += 1
                if global_step - initial_step > num_training_steps:
                    break

            if global_step - initial_step > num_training_steps:
                break

        self.logger.info("Finish training")

    def do_inference(self, data, batch_size=1, verbose=False):
        dataloader = data.get_dataloader(batch_size, is_training=False)
        self.logger.info("Dataloader(s) ready")
        if verbose:
            dataloader = tqdm(dataloader)
        losses = []
        # num_batches = ceil(num_examples * num_class_options / batch_size)
        for idx, batch in enumerate(dataloader):
            input_ids=batch[0].cuda()
            attention_mask=batch[1].cuda()
            token_type_ids=batch[2].cuda()
            if len(batch)==3:
                labels=None
            else:
                labels=batch[3].cuda()
            with torch.no_grad():
                loss = self.run_model(input_ids, attention_mask, token_type_ids, labels=labels)
            losses += loss.cpu().detach().numpy().tolist()
        return losses

    def do_predict(self, data, batch_size=1, losses=None, verbose=False):
        if losses is None:
            losses = self.do_inference(data, batch_size, verbose=verbose)
        losses = np.array(losses)
        assert len(losses)==len(data)
        predictions = []
        for idx, dp in enumerate(data.metadata):
            curr_label_losses = [np.sum(losses[indices]) for indices in dp["indices"]]
            prediction_idx = sorted(enumerate(curr_label_losses), key=lambda x: x[1])[0][0]
            prediction = dp["options"][prediction_idx]
            predictions.append(prediction.strip())
        return predictions

    def run_model(self, input_ids, attention_mask, token_type_ids, labels=None, label_smoothing=0.0):
        outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
        logits = outputs.logits[..., :-1, :].contiguous()

        if labels is None:
            labels = input_ids
        labels = labels[..., 1:].contiguous()
        label_mask = token_type_ids[..., 1:].contiguous()

        loss_fct = torch.nn.CrossEntropyLoss(reduction="none", label_smoothing=label_smoothing)
        losses = loss_fct(logits.view(-1, logits.size(-1)), labels.view(-1)) # [batch_size, length]

        losses = losses.view(logits.size(0), logits.size(1)) * label_mask
        return torch.sum(losses, axis=1) / torch.sum(label_mask, axis=1)

def setup_fp16(model, optimizer):
    try:
        import apex
        from apex import amp
        apex.amp.register_half_function(torch, "einsum")
    except ImportError:
        raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")

    fp16_opt_level = "O1"
    model, optimizer = amp.initialize(model, optimizer, opt_level=fp16_opt_level)
    return model, optimizer



