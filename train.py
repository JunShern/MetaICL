# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import os
import argparse
import pickle as pkl
import random
import torch
import math
import json
import string
import logging
import numpy as np

from pathlib import Path
from collections import Counter, defaultdict

import wandb
from transformers import GPT2Tokenizer, AutoTokenizer

from metaicl.data import MetaICLData
from metaicl.model import MetaICLModel
from utils.data import load_anydata

def get_gpu_memory_info(label="GPU memory usage"):
    free, total = torch.cuda.mem_get_info()
    reserved = total - free
    info = f"""{label}
    total: {total:,}
    reserved: {reserved:,}
    free: {free:,}
    """
    return info

def main(logger, args):
    if args.gpt2.startswith("gpt2"):
        tokenizer = GPT2Tokenizer.from_pretrained(args.gpt2)
    else:
        tokenizer = AutoTokenizer.from_pretrained("gpt2")

    if args.use_demonstrations:
        max_length = min(args.max_length * args.k, 1024)
    else:
        max_length = min(args.max_length, 1024)

    logger.info("batch_size=%d\tmax_length=%d\tmax_length_per_example=%d" % (
        args.batch_size, max_length, args.max_length_per_example))
    
    if args.keep_gpus_warm:
        logger.info(get_gpu_memory_info("GPU memory before allocation:"))
        total_memory = torch.cuda.get_device_properties(0).total_memory
        n_dummy_elements = int(total_memory * 0.5 / torch.rand(1).element_size())
        device = torch.device("cuda")
        dummy_arr = torch.rand(n_dummy_elements, device=device)
        logger.info(get_gpu_memory_info("GPU memory after allocation:"))

    train_data = load_anydata(args)
    logger.info(f"Num examples in the final set: {len(train_data)}")
    # Train data is a flat list of [json_obj, json_obj, json_obj, ...] where each json_obj is an example from relevant train.jsonl files
    num_tasks = len(set([dp["task"] for dp in train_data]))
    logger.info(f"Num tasks in the final set: {num_tasks}")

    train_counter = Counter()
    for dp in train_data:
        train_counter[dp["task"]] += 1
    if args.local_rank <= 0:
        for k, v in train_counter.items():
            logger.info("[Train] %s\t%d" % (k, v))
        logger.info("%s on %s (%d train)" % (args.method, args.task, len(train_counter)))

    if args.init_checkpoint is not None:
        assert os.path.exists(args.init_checkpoint)

    ######### tensorize data
    metaicl_data = MetaICLData(logger, tokenizer, args.method, args.use_demonstrations,
                               args.test_k, max_length, args.max_length_per_example,
                               do_tensorize=args.do_tensorize,
                               tensorize_dir=args.tensorize_dir,
                               n_process=args.n_process, n_gpu=args.n_gpu, local_rank=args.local_rank,
                               debug_data_order=args.debug_data_order,
                               shuffle=args.shuffle,
                               repeat_batch = args.repeat_batch)
    metaicl_data.tensorize_for_training(train_data, keyword=args.task, seed=args.seed)

    if args.keep_gpus_warm:
        logger.info(get_gpu_memory_info("GPU memory before freeing:"))
        del dummy_arr
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        logger.info(get_gpu_memory_info("GPU memory after freeing:"))

    # # TODO: This is terrible; either unify the functions or split them into entirely separate things!
    # ######### load tensorize data without do_tensorize
    # metaicl_data = MetaICLData(logger, tokenizer, args.method, args.use_demonstrations,
    #                            args.test_k, max_length, args.max_length_per_example,
    #                            do_tensorize=False,
    #                            tensorize_dir=args.tensorize_dir,
    #                            n_process=args.n_process, n_gpu=args.n_gpu, local_rank=args.local_rank,
    #                            debug_data_order=args.debug_data_order,
    #                            shuffle=args.shuffle)
    # metaicl_data.tensorize_for_training(train_data, keyword=args.task, seed=args.seed)

    ######## actual training part

    if 'SLURM_ARRAY_JOB_ID' in os.environ and 'SLURM_ARRAY_TASK_ID':
        slurm_job_id = f"{os.environ['SLURM_ARRAY_JOB_ID']}_{os.environ['SLURM_ARRAY_TASK_ID']}"
    elif 'SLURM_JOBID' in os.environ:
        slurm_job_id = os.environ['SLURM_JOBID']
    else:
        slurm_job_id = "localjob"

    # Setup wandb logging
    wandb.init(
        project="metaicl",
        tags=args.wandb_tags.split(',') if args.wandb_tags else None,
        mode='disabled' if args.disable_wandb else 'online',
    )
    wandb.run.name = f"{args.task}-{slurm_job_id}"
    wandb.config.update(args) # add all argparse args as config variables
    wandb.config.update({
        'slurm_job_id': slurm_job_id,
        'num_tasks': num_tasks,
    })

    random.seed(args.train_seed)
    np.random.seed(args.train_seed)
    torch.manual_seed(args.train_seed)
    if torch.cuda.device_count() > 0:
        torch.cuda.manual_seed_all(args.train_seed)

    if args.no_masking:
        metaicl_data.tensorized_inputs["token_type_ids"] = torch.ones_like(metaicl_data.tensorized_inputs["input_ids"])
    metaicl_data.print_tensorized_example()

    logger.info(args.out_dir)

    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)

    metaicl_model = MetaICLModel(
        logger, args.out_dir, args.fp16, args.local_rank,
        model_id=slurm_job_id, task=args.task, debug_data_order=args.debug_data_order,
        test_tasks=args.test_tasks, max_examples_per_test=args.max_examples_per_test,
        use_demonstrations=args.use_demonstrations, test_batch_size=args.test_batch_size, method=args.method)
    metaicl_model.load(args.init_checkpoint, args.gpt2)
    metaicl_model.to_device()
    metaicl_model.setup_optimizer(args.optimization, args.num_training_steps, args.lr,
                                  args.weight_decay, args.warmup_steps)
    metaicl_model.parallel()
    metaicl_model.train()
    metaicl_model.do_train(
        metaicl_data,
        args.batch_size,
        args.num_training_steps,
        save_period = args.save_period,
        log_period = args.log_period,
        log_period_epochs = args.log_period_epochs,
        gradient_accumulation_steps = args.gradient_accumulation_steps,
        max_grad_norm = args.max_grad_norm, 
        val_split = args.validation_split,
        label_smoothing = args.label_smoothing,
        verbose = args.verbose_train)

if __name__=='__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--do_tensorize", default=True, action="store_true")
    parser.add_argument("--tensorize_dir", type=str, default="tensorized")
    parser.add_argument("--n_gpu", type=int, default=1)
    parser.add_argument("--n_process", type=int, default=4)
    parser.add_argument("--max_length_per_example", type=int, default=256)
    parser.add_argument("--max_length", type=int, default=256)

    parser.add_argument("--use_demonstrations", type=int, default=1)
    parser.add_argument("--log_file", default=None, type=str)
    parser.add_argument("--debug_data_order", type=int, default=0)
    parser.add_argument("--repeat_batch", type=int, default=1)

    parser.add_argument("--num_training_steps", type=int, default=1000000)
    parser.add_argument("--validation_split", type=float, default=0.001)
    parser.add_argument("--save_period", type=int, default=None)
    parser.add_argument("--log_period", type=int, default=None)
    parser.add_argument("--log_period_epochs", type=int, default=None)

    parser.add_argument("--train_algo", type=str, default=None)
    parser.add_argument("--task", type=str, default="SST-2")
    parser.add_argument("--k", type=int, default=16384)
    parser.add_argument("--test_k", type=int, default=16)
    parser.add_argument("--seed", type=int, default=100)
    parser.add_argument("--train_seed", type=int, default=1)

    parser.add_argument("--max_tasks", type=int, default=None)
    parser.add_argument("--max_examples_per_task", type=int, default=None)
    parser.add_argument("--shuffle", type=int, default=1)
    parser.add_argument("--shuffle_examples_seed", type=int, default=0)

    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--warmup_steps", type=int, default=0)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    parser.add_argument("--test_batch_size", type=int, default=64)
    parser.add_argument("--max_grad_norm", type=float, default=1.0)
    parser.add_argument("--label_smoothing", type=float, default=0.0)

    parser.add_argument("--init_checkpoint", type=str, default=None)
    parser.add_argument("--weight_decay", type=float, default=0.0)
    parser.add_argument("--no_masking", default=False, action="store_true")

    parser.add_argument("--out_dir", type=str, default=None)
    parser.add_argument("--method", type=str, default="direct", choices=["direct", "channel"])
    parser.add_argument("--gpt2", type=str, default="gpt2-large")

    parser.add_argument("--optimization", type=str, default="8bit-adam")
    parser.add_argument("--fp16", default=True, action="store_true")
    parser.add_argument("--local_rank", type=int, default=-1, help="local_rank for distributed training on gpus")

    parser.add_argument("--wandb_tags", type=str, default=None)
    parser.add_argument("--disable_wandb", default=False, action="store_true")
    parser.add_argument("--verbose_train", type=int, default=0)

    parser.add_argument("--max_examples_per_test", type=int, default=100)
    parser.add_argument("--test_tasks", type=str, default=None) # 'all_tasks_test', 
    parser.add_argument("--is_cluster_dataset", type=int, default=0)
    parser.add_argument("--cluster_idxs", type=str, default=None)
    parser.add_argument("--max_tasks_per_cluster", type=int, default=None)

    parser.add_argument("--use_random_label", type=int, default=0)
    parser.add_argument("--predict_last_word", type=int, default=0)
    parser.add_argument("--swap_input_output", type=int, default=0)

    parser.add_argument("--target_num_examples", type=int, default=8000)
    parser.add_argument("--task_ratios", type=str, default=None)
    parser.add_argument("--keep_gpus_warm", type=int, default=0)

    args = parser.parse_args()

    if args.train_algo is None:
        args.train_algo = "metaicl" if args.use_demonstrations else "multitask-zero"
    if args.out_dir is None:
        args.train_algo = args.train_algo if args.method == "direct" else f"channel-{args.train_algo}"
        args.out_dir = f"checkpoints/{args.train_algo}/"
    
    if not (bool(args.log_period) ^ bool(args.log_period_epochs)): # NOT XOR
        # Only allow log_period XOR log_period_epochs
        print("Invalid arguments: log_period:{args.log_period}, log_period_epochs:{args.log_period_epochs}.\nApplying log_period_epochs=1.")
        args.log_period_epochs = 1

    handlers = [logging.StreamHandler()]
    if args.log_file is not None:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO,
                        handlers=handlers)
    logging.getLogger("transformers.tokenization_utils").setLevel(logging.ERROR)
    logger = logging.getLogger(__name__)
    logger.info(args)

    main(logger, args)
