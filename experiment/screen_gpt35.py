"""Offline preregistered screening of public GPT-3.5 EffiBench solutions."""
import ast
import hashlib
import json
from pathlib import Path

from run_effibench_main import ROOT, evaluate

SOURCE = ROOT / 'datasets' / 'effibench_gpt35.json'
OUT = ROOT / 'experiment' / 'results' / 'gpt35_screen.jsonl'


def eligible(row):
    try:
        body = ast.parse(row['test_case']).body
        return (len(body) >= 30 and all(isinstance(n, ast.Assert) for n in body)
                and 'class Solution' in row['completion'])
    except (SyntaxError, KeyError):
        return False


def main():
    rows = [row for row in json.loads(SOURCE.read_text(encoding='utf-8')) if eligible(row)]
    rows.sort(key=lambda row: hashlib.sha256(str(row['problem_idx']).encode()).hexdigest())
    done = {json.loads(line)['problem_idx'] for line in OUT.open(encoding='utf-8')} if OUT.exists() else set()
    for row in rows:
        if row['problem_idx'] in done:
            continue
        canonical = evaluate(row['canonical_solution'], row, timeout=15)
        baseline = evaluate(row['completion'], row, timeout=15)
        item = {'problem_idx': row['problem_idx'], 'task_name': row['task_name'],
                'canonical_correct': canonical.get('correct', False),
                'base_correct': baseline.get('correct', False),
                'canonical_ns': canonical.get('total_ns'), 'base_ns': baseline.get('total_ns'),
                'canonical_error': canonical.get('error'), 'base_error': baseline.get('error')}
        if item['canonical_correct'] and item['base_correct'] and item['canonical_ns']:
            item['ratio'] = round(item['base_ns'] / item['canonical_ns'], 3)
            item['admit'] = item['ratio'] >= 2.5 and item['base_ns'] >= 1_000_000
        else:
            item['admit'] = False
        with OUT.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        n = sum(1 for _ in OUT.open(encoding='utf-8'))
        if n % 25 == 0 or item['admit']:
            print('screened', n, 'admitted', item['problem_idx'] if item['admit'] else '-', flush=True)


if __name__ == '__main__':
    main()
