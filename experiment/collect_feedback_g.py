"""Validate and freeze progressive feedback before any G0-G3 model calls."""
import json
import statistics
import time
from pathlib import Path

from run_effibench_main import ROOT, evaluate
from run_effibench_v2 import SOURCE, OUT, PROFILE, run_json

DEST = ROOT / 'experiment/results/g_feedback_validation.json'


def main():
    prior = json.loads(OUT.read_text(encoding='utf-8'))
    ids = {int(k) for k, v in prior.items() if v.get('status') == 'complete'}
    source = {r['problem_idx']: r for r in json.loads(SOURCE.read_text(encoding='utf-8'))
              if r['problem_idx'] in ids}
    state = json.loads(DEST.read_text(encoding='utf-8')) if DEST.exists() else {}
    for key, old in prior.items():
        if old.get('status') != 'complete' or key in state:
            continue
        row = source[int(key)]
        start = time.perf_counter()
        runs = [evaluate(row['completion'], row, timeout=25) for _ in range(3)]
        timing_collection_s = time.perf_counter() - start
        rec = {'problem_idx': int(key), 'group': old['group'],
               'timing_collection_s': round(timing_collection_s, 4)}
        if all(x.get('correct') and x.get('n_tests') == runs[0].get('n_tests') for x in runs):
            by_index = {}
            for run in runs:
                for test in run['slowest']:
                    by_index.setdefault(test['index'], []).append(test)
            # The worker reports only top-five tests; a stable candidate must appear in all runs.
            stable = [(i, statistics.median(x['ns'] for x in tests), tests[0]['test'])
                      for i, tests in by_index.items() if len(tests) == 3]
            rec['total_ns'] = round(statistics.median(x['total_ns'] for x in runs))
            rec['total_range_fraction'] = round((max(x['total_ns'] for x in runs) -
                                                 min(x['total_ns'] for x in runs)) / rec['total_ns'], 3)
            if stable:
                index, ns, test = max(stable, key=lambda x: x[1])
                rec['slowest'] = {'index': index, 'median_ns': round(ns), 'test': test}
                start = time.perf_counter()
                profiles = [run_json(PROFILE, {'code': row['completion'], 'test_case': row['test_case'],
                                              'index': index}, 12) for _ in range(2)]
                rec['profile_collection_s'] = round(time.perf_counter() - start, 4)
                if all(p.get('lines') for p in profiles):
                    first, second = profiles
                    rec['profile'] = first
                    rec['top_line_stable'] = first['lines'][0]['line'] == second['lines'][0]['line']
                    rec['profile_runs_top_lines'] = [p['lines'][0]['line'] for p in profiles]
                    rec['usable'] = rec['total_range_fraction'] <= 0.5 and rec['top_line_stable']
                else:
                    rec['profile_error'] = [p.get('error') for p in profiles]
                    rec['usable'] = False
            else:
                rec['error'] = 'no slow test appeared in all three timing runs'
                rec['usable'] = False
        else:
            rec['error'] = 'baseline correctness or test count varied'
            rec['usable'] = False
        state[key] = rec
        DEST.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        print('feedback', key, 'usable' if rec['usable'] else 'exclude', flush=True)


if __name__ == '__main__':
    main()
