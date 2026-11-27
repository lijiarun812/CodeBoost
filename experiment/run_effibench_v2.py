"""Paired feedback experiment on frozen inefficient, correct public baselines."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import llm_client
from run_effibench_main import ROOT, evaluate

SCREEN = ROOT / 'experiment/results/gpt35_screen.jsonl'
SOURCE = ROOT / 'datasets/effibench_gpt35.json'
MANIFEST = ROOT / 'experiment/results/effibench_v2_manifest.json'
OUT = ROOT / 'experiment/results/effibench_v2_results.json'
PROFILE = Path(__file__).with_name('line_profile_worker.py')
HOLDOUT = Path(__file__).with_name('holdout_worker.py')
METHODS = ('vanilla', 'perfcodegen', 'ours', 'effilearner_time')


def save(data):
    tmp = OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(OUT)


def run_json(worker, payload, timeout):
    try:
        proc = subprocess.run([sys.executable, str(worker)], input=json.dumps(payload),
                              text=True, capture_output=True, encoding='utf-8', timeout=timeout)
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {'error': type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true', help='validate baselines and holdout tests without model calls')
    args = parser.parse_args()
    screened = [json.loads(line) for line in SCREEN.open(encoding='utf-8')]
    admitted = [x for x in screened if x['admit']]
    manifest = [{'problem_idx': x['problem_idx'], 'group': 'main' if x['base_ns'] >= 5_000_000 else 'short_control'}
                for x in admitted]
    if MANIFEST.exists():
        if json.loads(MANIFEST.read_text(encoding='utf-8')) != manifest:
            raise RuntimeError('Frozen manifest differs from screening results')
    else:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = {r['problem_idx']: r for r in json.loads(SOURCE.read_text(encoding='utf-8'))
            if r['problem_idx'] in {x['problem_idx'] for x in admitted}}
    state = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    for entry in manifest:
        idx = entry['problem_idx']
        key = str(idx)
        row = rows[idx]
        rec = state.setdefault(key, {'problem_idx': idx, 'group': entry['group'],
                                     'task_name': row['task_name'], 'model': 'qwen4-max',
                                     'baseline_source': 'EffiBench public GPT-3.5 result'})
        if rec.get('status') in ('complete', 'invalid_baseline', 'invalid_holdout'):
            continue
        base = row['completion']
        if 'base_check' not in rec:
            rec['base_check'] = evaluate(base, row, timeout=25)
            if not rec['base_check'].get('correct'):
                rec['status'] = 'invalid_baseline'
                save(state)
                continue
            generated = run_json(HOLDOUT, row, 25)
            rec['holdout_generator_error'] = generated.get('error')
            if generated.get('tests'):
                holdout = '\n'.join(generated['tests'])
                rec['holdout_test'] = holdout
                rec['holdout_canonical'] = evaluate(row['canonical_solution'], {'test_case': holdout}, timeout=15)
                rec['holdout_base'] = evaluate(base, {'test_case': holdout}, timeout=15)
            if not rec.get('holdout_canonical', {}).get('correct') or not rec.get('holdout_base', {}).get('correct'):
                rec['status'] = 'invalid_holdout'
                save(state)
                continue
            slow = rec['base_check']['slowest'][0]
            rec['profile'] = run_json(PROFILE, {'code': base, 'test_case': row['test_case'], 'index': slow['index']}, 10)
            save(state)
        if args.preflight:
            print('preflight', idx, 'valid', flush=True)
            continue
        check = rec['base_check']
        slow = check['slowest'][0]
        profile = rec.get('profile', {}).get('lines', [])
        task = re.sub(r'<[^>]+>', ' ', row['description'])[:4500]
        common = (f'Task:\n{task}\n\nCurrent correct code:\n{base}\n\n'
                  'Rewrite the complete class Solution for faster execution. Preserve correctness. '
                  'Return Python code only.\n')
        feedback = {
            'vanilla': '',
            'perfcodegen': f"Slowest observed test: {slow['test']}\nElapsed: {slow['ns']} ns.",
            'ours': ('Five most expensive observed tests:\n' +
                     '\n'.join(f"{x['ns']} ns: {x['test']}" for x in check['slowest']) +
                     '\nApproximate hot source lines on the slowest test:\n' +
                     ('\n'.join(f"line {x['line']}: {x['source']} ({x['profile_ns']} profile ns)"
                                for x in profile) or 'profile unavailable')),
            'effilearner_time': f"Total measured execution time for all {check['n_tests']} tests: {check['total_ns']} ns. Reduce this overhead.",
        }
        for method in METHODS:
            if method in rec:
                continue
            try:
                code = llm_client.chat(common + feedback[method], max_tokens=2048)
            except llm_client.FreeQuotaExhausted:
                rec['status'] = 'quota_exhausted'
                save(state)
                print('free quota exhausted; stopped', flush=True)
                return
            except Exception as exc:
                rec['status'] = 'api_error'
                rec['error'] = f'{type(exc).__name__}: {str(exc)[:300]}'
                save(state)
                print('API error', idx, flush=True)
                return
            rec[method] = {'raw': code,
                           'original_check': evaluate(code, row, timeout=25),
                           'holdout_check': evaluate(code, {'test_case': rec['holdout_test']}, timeout=15)}
            save(state)
            print(idx, method, 'done', flush=True)
        rec['status'] = 'complete'
        save(state)


if __name__ == '__main__':
    main()
