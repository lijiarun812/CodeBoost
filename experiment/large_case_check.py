"""Offline stress checks for two extreme-speedup cases from the first run."""
import json
from pathlib import Path

from run_effibench_main import DATA, OUT, evaluate

DEST = OUT.with_name('large_case_checks.json')


def main():
    rows = {r['problem_idx']: r for line in DATA.open(encoding='utf-8')
            if (r := json.loads(line))['problem_idx'] in (809, 1467)}
    records = {r['problem_idx']: r for line in OUT.open(encoding='utf-8')
               if (r := json.loads(line))['problem_idx'] in rows}
    results = {}
    for idx, row in rows.items():
        ns = {}
        exec('from typing import *\nfrom collections import *\nfrom functools import *\nfrom itertools import *\nfrom math import *', ns)
        exec(row['canonical_solution'], ns)
        if idx == 809:
            s = 'a' * 400 + 'b' * 400
            words = ['a' * (2 + i % 10) + 'b' * (2 + i % 8) for i in range(2000)]
            words += ['ab', 'a' * 400 + 'b' * 400] * 50
            expected = ns['Solution']().expressiveWords(s, words)
            test = f'assert solution.expressiveWords({s!r}, {words!r}) == {expected!r}'
        else:
            balls = [2] * 8
            expected = ns['Solution']().getProbability(balls)
            test = f'assert abs(solution.getProbability({balls!r}) - {expected!r}) < 1e-9'
        check_row = {'test_case': test}
        rec = records[idx]
        result = {'problem_idx': idx, 'test_description': 'large deterministic input', 'methods': {}}
        for label, code in [('base', rec.get('repair_raw', rec['base_raw']))] + [
            (m, rec[m + '_raw']) for m in ('vanilla', 'perfcodegen', 'ours')
        ]:
            runs = [evaluate(code, check_row, timeout=20) for _ in range(3)]
            result['methods'][label] = {
                'correct': all(x.get('correct') for x in runs),
                'ms': [round(x['total_ns'] / 1e6, 3) for x in runs if x.get('correct')],
                'error': next((x.get('error') or x.get('failed_test') for x in runs if not x.get('correct')), None),
            }
        results[str(idx)] = result
        DEST.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print('large', idx, result['methods'], flush=True)


if __name__ == '__main__':
    main()
