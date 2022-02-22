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

from tqdm import tqdm
from collections import Counter, defaultdict

from torch.utils.data import TensorDataset, DataLoader, SequentialSampler
from transformers import GPT2Tokenizer, AutoTokenizer

from metaicl.data import MetaICLData

from utils.data import load_data

def main(logger, args, metaicl_model=None):
    if args.gpt2.startswith("gpt2"):
        tokenizer = GPT2Tokenizer.from_pretrained(args.gpt2)
    else:
        tokenizer = AutoTokenizer.from_pretrained("gpt2")
    add_newlines = True

    ### checkpoint ...
    if not args.do_zeroshot:
        if args.checkpoint is not None:
            checkpoint = args.checkpoint
            assert args.global_step is None
        else:
            assert args.global_step is not None
            checkpoint = os.path.join(args.out_dir, "model-{}.pt".format(args.global_step))
        assert os.path.exists(checkpoint)
    else:
        checkpoint = None
        add_newlines = args.gpt2=="gpt-j-6B"
    if metaicl_model is None:
        # This test function may be called from MetaICLModel, in which case
        # don't import this (to avoid circular dependencies)
        from metaicl.model import MetaICLModel
        metaicl_model = MetaICLModel(logger, args.out_dir)

    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)

    # setup hyperparams for data

    max_length_per_example = 256
    max_length = 256
    if args.use_demonstrations:
        orig_max_length = max_length
        if args.do_zeroshot:
            max_length = min(max_length * args.k, 1024)
        else:
            max_length = min(max_length * args.k, 1024)

    logger.info("batch_size=%d\tmax_length=%d\tmax_length_per_example=%d" % (
        args.test_batch_size, max_length, max_length_per_example))

    metaicl_data = MetaICLData(logger, tokenizer, args.method,args.use_demonstrations, args.k,
                               max_length, max_length_per_example)

    results = []
    errors = []
    seeds = args.seed.split(",")
    config_split = "unseen_domain_test" if args.unseen_domain_only else "test"

    # Load the test tasks to evaluate on
    dev_data = load_data(args.task, args.split, args.k, seed=100, config_split=config_split,
        is_null=args.is_null, max_examples_per_task=args.max_examples_per_task)
    dev_counter = Counter()
    for dp in dev_data:
        dev_counter[dp["task"]] += 1
    for k, v in dev_counter.items():
        logger.info("[Dev] %s\t%d" % (k, v))

    results_dict = {}
    for task_idx, test_task in enumerate(dev_counter):
        seed = seeds[task_idx % len(seeds)] # Arbitrarily choose one random seed (for sampling k-shot context)

        # Load the corresponding k-shot context for the chosen seed
        train_data = load_data(args.task, "train", args.k, seed=seed, config_split=config_split)

        logger.info(f"--------------------- SEED {seed} | TEST TASK ({task_idx} / {len(dev_counter)}): {test_task}")
        curr_dev_data = [dp for dp in dev_data if dp["task"]==test_task]
        curr_train_data = [dp for dp in train_data if dp["task"]==test_task]
        assert len(curr_dev_data)>0
        assert not args.use_demonstrations or len(curr_train_data)==args.k, \
                (args.use_demonstrations, len(curr_train_data), args.k)

        config_file = "config/tasks/{}.json".format(test_task)
        assert os.path.exists(config_file), config_file
        with open(config_file, "r") as f:
            config = json.load(f)
        is_classification = config["task_type"]=="classification"
        if is_classification:
            options = curr_dev_data[0]["options"]
            assert np.all([d["options"]==options for d in curr_dev_data+curr_train_data])

        result = run(args, logger, test_task, metaicl_data, metaicl_model,
                        curr_train_data, curr_dev_data, seed, checkpoint, is_classification, add_newlines)

        if result is None:
            errors.append("%s/%s" % (test_task, seed))
        else:
            results_dict[test_task] = result
            results.append(result)

    if args.is_null:
        return

    logger.info("Macro-F1 of %s over %d target tasks: %.1f" % (args.task, len(results) // len(seeds), 100*np.mean(results)))
    results_dict['mean'] = np.mean(results)

    if len(errors)>0:
        logger.info("You had errors with datasets:", ",".join(errors))
        logger.info("Please see the error messages")
    return results_dict


def run(args, logger, task, metaicl_data, metaicl_model, train_data, dev_data, seed,
        checkpoint, is_classification, add_newlines):

    if args.do_zeroshot:
        split_name = args.split
        if args.is_null:
            split_name += "-null"
        cache_path = os.path.join(args.out_dir,
                                  "{}-{}-{}{}{}{}.pkl".format(
                                      task,
                                      split_name,
                                      metaicl_data.method,
                                      "-k={}".format(args.k) if args.use_demonstrations else "",
                                      "-s={}".format(seed) if args.use_demonstrations else "",
                                      "" if add_newlines else "-no-newlines"))
    else:
        assert add_newlines
        cache_path = os.path.join(args.out_dir, "{}-{}-{}{}{}.pkl".format(
                        task,
                        args.split,
                        metaicl_data.method,
                        "-k={}".format(args.k) if args.use_demonstrations else "",
                        "-s={}".format(seed) if args.use_demonstrations else ""
                      ))

    metaicl_data.tensorize(train_data, dev_data, add_newlines=add_newlines)
    # metaicl_data.print_tensorized_example()
    logger.info(cache_path)

    # Disable caching: very error-prone if you run new experiments while forgetting to delete the cache
    # if os.path.exists(cache_path):
    #     with open(cache_path, "rb") as f:
    #         losses = pkl.load(f)
    # else:
    if metaicl_model.is_none():
        metaicl_model.load(checkpoint)
        metaicl_model.cuda()
        metaicl_model.eval()

    losses = metaicl_model.do_inference(metaicl_data, args.test_batch_size)
    with open(cache_path, "wb") as f:
        pkl.dump(losses, f)

    assert len(losses)==len(metaicl_data)

    if args.is_null:
        return None

    if args.use_calibration:
        assert args.do_zeroshot
        bias_path = cache_path.replace("/"+task+"-"+args.split, "/"+task+"-"+args.split+"-null")
        assert os.path.exists(bias_path), bias_path
        with open(bias_path, "rb") as f:
            bias_losses = pkl.load(f)

        losses = np.array(losses)
        bias_losses = np.array(bias_losses)
        assert losses.shape == bias_losses.shape
        losses -= bias_losses

    predictions = metaicl_model.do_predict(metaicl_data, losses=losses)
    groundtruths = [dp["output"] for dp in dev_data]
    perf = metaicl_data.evaluate(predictions, groundtruths, is_classification)
    logger.info("Accuracy=%s" % perf)
    return perf

if __name__=='__main__':

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

    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file is not None:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO,
                        handlers=handlers)
    logger = logging.getLogger(__name__)
    logger.info(args)

    main(logger, args)
