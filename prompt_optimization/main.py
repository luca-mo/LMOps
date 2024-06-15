import requests
import os
import evaluators
import concurrent.futures
from tqdm import tqdm
import time
import json
import argparse
import scorers
import tasks
import predictors
import optimizers


def get_task_class(task_name):
    if task_name == 'ethos':
        return tasks.EthosBinaryTask
    elif task_name == 'jailbreak':
        return tasks.JailbreakBinaryTask
    elif task_name == 'liar':
        return tasks.DefaultHFBinaryTask
    elif task_name == 'ar_sarcasm':
        return tasks.DefaultHFBinaryTask
    elif task_name == 'bullying':
        return tasks.DefaultHFBinaryTask
    elif task_name == 'toxic':
        return tasks.DefaultHFBinaryTask
    else:
        raise Exception(f'Unsupported task: {task_name}')


def get_evaluator(evaluator):
    if evaluator == 'bf':
        return evaluators.BruteForceEvaluator
    elif evaluator in {'ucb', 'ucb-e'}:
        return evaluators.UCBBanditEvaluator
    elif evaluator in {'sr', 's-sr'}:
        return evaluators.SuccessiveRejectsEvaluator
    elif evaluator == 'sh':
        return evaluators.SuccessiveHalvingEvaluator
    else:
        raise Exception(f'Unsupported evaluator: {evaluator}')



def get_scorer(scorer):
    if scorer == '01':
        return scorers.Cached01Scorer
    elif scorer == 'll':
        return scorers.CachedLogLikelihoodScorer
    else:
        raise Exception(f'Unsupported scorer: {scorer}')


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', default='ethos')
    parser.add_argument('--data_dir', default='data/ethos')
    parser.add_argument('--prompts', default='prompts/ethos.md')
    # parser.add_argument('--config', default='default.json')
    parser.add_argument('--out', default='test_out.txt')
    parser.add_argument('--max_threads', default=32, type=int)
    parser.add_argument('--temperature', default=0.0, type=float) # Set to 0.0 for few-shot

    parser.add_argument('--optimizer', default='nl-gradient')
    parser.add_argument('--rounds', default=6, type=int) # Search depth r
    parser.add_argument('--beam_size', default=4, type=int) # Only the top n prompt variations are retained
    parser.add_argument('--n_test_exs', default=400, type=int) # Prompt is tested on n examples of the test set

    parser.add_argument('--minibatch_size', default=64, type=int) # Samples n examples from the training set without replacement
    parser.add_argument('--n_gradients', default=4, type=int) # For each minibatch the optimizer generates n feedbacks (the next 3
    parser.add_argument('--errors_per_gradient', default=4, type=int) # Select up to n false predictions from the minibatch
    parser.add_argument('--gradients_per_error', default=1, type=int) # Each error sampled will result in one feedback point
    parser.add_argument('--steps_per_gradient', default=1, type=int) # For each feedback on an error, n new variations are produced
    parser.add_argument('--mc_samples_per_step', default=2, type=int) # For each modified prompt, the optimizer generates n new variations
    parser.add_argument('--max_expansion_factor', default=8, type=int) # No more than n new prompts will be retained after each round

    parser.add_argument('--engine', default="chatgpt", type=str)

    parser.add_argument('--evaluator', default="bf", type=str) # UCB etc
    parser.add_argument('--scorer', default="01", type=str) # Binary classificaton
    parser.add_argument('--eval_rounds', default=8, type=int) # Rounds in the bandit selection, T (time steps) in the algorithm
    parser.add_argument('--eval_prompts_per_round', default=8, type=int) # n prompts are evaluated in each round
    # calculated by s-sr and sr
    parser.add_argument('--samples_per_eval', default=32, type=int) # Prompts are tested on n samples, similar to development set
    parser.add_argument('--c', default=1.0, type=float, help='exploration param for UCB. higher = more exploration')
    parser.add_argument('--knn_k', default=2, type=int)
    parser.add_argument('--knn_t', default=0.993, type=float)
    parser.add_argument('--reject_on_errors', action='store_true')

    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = get_args()

    config = vars(args)

    config['eval_budget'] = config['samples_per_eval'] * config['eval_rounds'] * config['eval_prompts_per_round']

    task = get_task_class(args.task)(args.data_dir, args.max_threads)
    scorer = get_scorer(args.scorer)()
    evaluator = get_evaluator(args.evaluator)(config)
    bf_eval = get_evaluator('bf')(config)
    gpt4 = predictors.BinaryPredictor(config)

    optimizer = optimizers.ProTeGi(
        config, evaluator, scorer, args.max_threads, bf_eval)

    train_exs = task.get_train_examples()
    test_exs = task.get_test_examples()

    if os.path.exists(args.out):
        os.remove(args.out)

    print(config)

    with open(args.out, 'a') as outf:
        outf.write(json.dumps(config) + '\n')

    candidates = [open(fp.strip()).read() for fp in args.prompts.split(',')]

    for round in tqdm(range(config['rounds'] + 1)):
        print("STARTING ROUND ", round)
        start = time.time()

        # expand candidates
        if round > 0:
            candidates = optimizer.expand_candidates(candidates, task, gpt4, train_exs)

        # score candidates
        scores = optimizer.score_candidates(candidates, task, gpt4, train_exs)
        [scores, candidates] = list(zip(*sorted(list(zip(scores, candidates)), reverse=True)))

        # select candidates
        candidates = candidates[:config['beam_size']]
        scores = scores[:config['beam_size']]

        # record candidates, estimated scores, and true scores
        with open(args.out, 'a') as outf:
            outf.write(f"======== ROUND {round}\n")
            outf.write(f'{time.time() - start}\n')
            outf.write(f'{candidates}\n')
            outf.write(f'{scores}\n')
        metrics = []
        for candidate, score in zip(candidates, scores):
            f1, texts, labels, preds = task.evaluate(gpt4, candidate, test_exs, n=args.n_test_exs)
            metrics.append(f1)
        with open(args.out, 'a') as outf:
            outf.write(f'{metrics}\n')

    print("DONE!")
