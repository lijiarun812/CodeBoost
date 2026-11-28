"""Frozen 30-task EffiBench main experiment: Base + three paired refinements."""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import llm_client

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'datasets' / 'effibench.jsonl'
OUT = ROOT / 'experiment' / 'results' / 'effibench_main.jsonl'
MANIFEST = ROOT / 'experiment' / 'results' / 'effibench_manifest.json'
WORKER = Path(__file__).with_name('effibench_worker.py')


def candidates():
    rows = [json.loads(line) for line in DATA.open(encoding='utf-8')]
    eligible = []
    for row in rows:
        try:
            body = ast.parse(row['test_case']).body
            canon = ast.parse(row['canonical_solution']).body
        except (SyntaxError, KeyError):
            continue
        if (len(body) >= 30 and all(isinstance(n, ast.Assert) for n in body)
                and any(isinstance(n, ast.ClassDef) and n.name == 'Solution' for n in canon)
                and len(row['canonical_solution']) >= 400):
            eligible.append(row)
    eligible.sort(key=lambda r: hashlib.sha256(str(r['problem_idx']).encode()).hexdigest())
    return eligible[:30]


def evaluate(code, row, timeout=25, repeats=1):
    try:
        proc = subprocess.run([sys.executable, str(WORKER)],
                              input=json.dumps({'code': code, 'test_case': row['test_case'], 'repeats': repeats}),
                              text=True, capture_output=True, encoding='utf-8', timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'correct': False, 'timeout': True, 'error': f'timeout after {timeout}s'}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {'correct': False, 'error': f'worker exit {proc.returncode}: {proc.stderr[-200:]}'}


def generate(prompt):
    return llm_client.chat(prompt, max_tokens=2048)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=30)
    args = parser.parse_args()
    selected = candidates()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    if not MANIFEST.exists():
        MANIFEST.write_text(json.dumps([{'problem_idx': r['problem_idx'], 'task_name': r['task_name']}
                                        for r in selected], ensure_ascii=False, indent=2), encoding='utf-8')
    expected = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if [r['problem_idx'] for r in selected] != [r['problem_idx'] for r in expected]:
        raise RuntimeError('Frozen task manifest differs from dataset')
    done = set()
    if OUT.exists():
        done = {json.loads(line)['problem_idx'] for line in OUT.open(encoding='utf-8') if line.strip()}
    for row in selected[:args.limit]:
        idx = row['problem_idx']
        if idx in done:
            continue
        canonical = evaluate(row['canonical_solution'], row)
        record = {'problem_idx': idx, 'task_name': row['task_name'], 'canonical_check': canonical}
        if not canonical.get('correct'):
            record['status'] = 'canonical_failed'
        else:
            task = re.sub(r'<[^>]+>', ' ', row['description'])[:4500]
            base_prompt = (f'Write a complete Python class Solution for this task. Return only Python code.\n{task}')
            try:
                base = generate(base_prompt)
                record['base_raw'] = base
                base_result = evaluate(base, row)
                record['base_check'] = base_result
                if not base_result.get('correct'):
                    base = generate(f'Fix this solution for the task. Return complete class Solution only.\n'
                                    f'Task: {task}\nCode: {base}\nFailure: {base_result}')
                    record['repair_raw'] = base
                    base_result = evaluate(base, row)
                    record['repair_check'] = base_result
                if base_result.get('correct'):
                    slowest = base_result['slowest'][0]
                    shared = f'Task:\n{task}\n\nCurrent code:\n{base}\n\nReturn a complete faster class Solution. Preserve correctness.'
                    feedback = {
                        'vanilla': '',
                        'perfcodegen': f"Slowest test: {slowest['test']}\nTime: {slowest['ns']} ns.",
                        'ours': ('Measured execution feedback, top five expensive tests:\n' +
                                 '\n'.join(f"{x['ns']} ns: {x['test']}" for x in base_result['slowest']) +
                                 f"\nTotal test time: {base_result['total_ns']} ns."),
                    }
                    for method, extra in feedback.items():
                        candidate = generate(shared + '\n' + extra)
                        record[method + '_raw'] = candidate
                        record[method + '_check'] = evaluate(candidate, row)
                    record['status'] = 'complete'
                else:
                    record['status'] = 'base_wrong'
            except llm_client.FreeQuotaExhausted:
                print('Free quota exhausted; stopping without retry', flush=True)
                break
            except Exception as exc:
                record['status'] = 'api_error'
                record['error'] = f'{type(exc).__name__}: {str(exc)[:300]}'
        with OUT.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        print(f"{idx} {record['status']}", flush=True)


if __name__ == '__main__':
    main()
