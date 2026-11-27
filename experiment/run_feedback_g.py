"""G0-G3: identical optimization instruction, nested execution feedback."""
import argparse
import ast
import json
import re
from pathlib import Path

import llm_client
from run_effibench_main import ROOT, evaluate
from run_effibench_v2 import SOURCE, OUT as PRIOR

FEEDBACK = ROOT / 'experiment/results/g_feedback_validation.json'
MANIFEST = ROOT / 'experiment/results/g_manifest.json'
OUT = ROOT / 'experiment/results/g_results.json'
GROUPS = ('G0', 'G1', 'G2', 'G3')
SYSTEM = 'You are an expert Python developer.'
INSTRUCTION = ('Rewrite the complete class Solution for faster execution while preserving '
               'correctness on all valid inputs. Return only Python code.')


def prompt_for(row, baseline, feedback, group):
    task = re.sub(r'<[^>]+>', ' ', row['description'])[:4500]
    base = (f'Task:\n{task}\n\nCurrent correct code:\n{baseline}\n\n'
            f'Instruction: {INSTRUCTION}\n\nExecution feedback:\n')
    level1 = (f"Total execution time across {len(ast.parse(row['test_case']).body)} "
              f"fixed tests: {feedback['total_ns']} ns.\n")
    slow = feedback['slowest']
    level2 = level1 + f"Slowest test: {slow['test']}\nTime on that test: {slow['median_ns']} ns.\n"
    lines = feedback['profile']['lines'][:5]
    level3 = level2 + 'Approximate line hotspots for the slowest test (profiled run):\n' + '\n'.join(
        f"line {x['line']}: {x['source']} | hits={x['hits']} | profile_ns={x['profile_ns']}"
        for x in lines) + '\n'
    blocks = {'G0': '', 'G1': level1, 'G2': level2, 'G3': level3}
    assert level2.startswith(level1) and level3.startswith(level2)
    return base + blocks[group], blocks[group]


def save(data):
    temporary = OUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(OUT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--repeats', type=int, default=2)
    args = parser.parse_args()
    if args.repeats != 2:
        raise ValueError('This registered experiment uses exactly two generations per group')
    prior = json.loads(PRIOR.read_text(encoding='utf-8'))
    feedback = json.loads(FEEDBACK.read_text(encoding='utf-8'))
    selected = [{'problem_idx': int(k), 'group': v['group']}
                for k, v in feedback.items() if v['usable']]
    if MANIFEST.exists():
        if json.loads(MANIFEST.read_text(encoding='utf-8')) != selected:
            raise RuntimeError('Frozen G0-G3 manifest differs')
    else:
        MANIFEST.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding='utf-8')
    source = {r['problem_idx']: r for r in json.loads(SOURCE.read_text(encoding='utf-8'))
              if r['problem_idx'] in {x['problem_idx'] for x in selected}}
    state = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    for position, item in enumerate(selected):
        idx = item['problem_idx']
        key = str(idx)
        row = source[idx]
        baseline = row['completion']
        prompts = {group: prompt_for(row, baseline, feedback[key], group) for group in GROUPS}
        common = prompts['G0'][0]
        assert all(prompts[g][0].startswith(common) for g in GROUPS)
        if args.preflight:
            print('prompt_valid', idx, [len(prompts[g][1]) for g in GROUPS], flush=True)
            continue
        record = state.setdefault(key, {'problem_idx': idx, 'group': item['group'],
                                        'baseline_source': 'EffiBench GPT-3.5 public solution',
                                        'model': 'qwen4-max', 'generations': {g: {} for g in GROUPS},
                                        'holdout_test': prior[key]['holdout_test']})
        for replicate in range(2):
            order = GROUPS[(position + replicate) % 4:] + GROUPS[:(position + replicate) % 4]
            for group in order:
                tag = str(replicate + 1)
                if tag in record['generations'][group]:
                    continue
                prompt, block = prompts[group]
                try:
                    answer = llm_client.chat_with_meta(prompt, system=SYSTEM, max_tokens=2048)
                except llm_client.FreeQuotaExhausted:
                    record['status'] = 'free_quota_exhausted'
                    save(state)
                    print('free quota exhausted; stopped', flush=True)
                    return
                except Exception as exc:
                    record['status'] = 'api_error'
                    record['error'] = f'{type(exc).__name__}: {str(exc)[:300]}'
                    save(state)
                    print('API error', idx, group, flush=True)
                    return
                code = answer.pop('content')
                record['generations'][group][tag] = {
                    'prompt': prompt, 'feedback': block, 'code': code,
                    'api': answer,
                    'original_check': evaluate(code, row, timeout=25),
                    'holdout_check': evaluate(code, {'test_case': record['holdout_test']}, timeout=15),
                }
                save(state)
                print(idx, group, 'rep', tag, flush=True)
        record['status'] = 'complete'
        save(state)


if __name__ == '__main__':
    main()
