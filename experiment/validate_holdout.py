"""Offline validation of existing solutions on generated, unseen inputs."""
import json
import subprocess
import sys
from pathlib import Path

from run_effibench_main import DATA, OUT, evaluate

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'experiment' / 'results' / 'effibench_holdout.json'
WORKER = Path(__file__).with_name('holdout_worker.py')


def main():
    records = [json.loads(x) for x in OUT.open(encoding='utf-8')]
    ids = {r['problem_idx'] for r in records if r['status'] == 'complete'}
    tasks = {r['problem_idx']: r for line in DATA.open(encoding='utf-8')
             if (r := json.loads(line))['problem_idx'] in ids}
    results = json.loads(DEST.read_text(encoding='utf-8')) if DEST.exists() else {}
    for record in records:
        idx = record['problem_idx']
        if record['status'] != 'complete' or (str(idx) in results and not results[str(idx)].get('generator_error')):
            continue
        row = tasks[idx]
        try:
            proc = subprocess.run([sys.executable, str(WORKER)], input=json.dumps(row),
                                  text=True, capture_output=True, encoding='utf-8', timeout=25)
            generated = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, ValueError) as exc:
            generated = {'error': type(exc).__name__}
        item = {'problem_idx': idx, 'generator_error': generated.get('error')}
        if generated.get('tests'):
            holdout = '\n'.join(generated['tests'])
            canonical = evaluate(row['canonical_solution'], {'test_case': holdout}, timeout=15)
            item['canonical'] = canonical.get('correct', False)
            item['test_count'] = len(generated['tests'])
            if item['canonical']:
                for label, code in [('base', record.get('repair_raw', record['base_raw']))] + [
                    (m, record[m + '_raw']) for m in ('vanilla', 'perfcodegen', 'ours')
                ]:
                    check = evaluate(code, {'test_case': holdout}, timeout=15)
                    item[label] = {'correct': check.get('correct', False),
                                   'error': check.get('error') or check.get('failed_test')}
        results[str(idx)] = item
        DEST.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print('holdout', idx, 'valid' if item.get('canonical') else 'unusable', flush=True)


if __name__ == '__main__':
    main()
