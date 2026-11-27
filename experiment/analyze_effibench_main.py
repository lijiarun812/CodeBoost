"""Recheck correctness and measure paired whole-suite runtimes without API calls."""
import json
import statistics
from pathlib import Path

from run_effibench_main import DATA, OUT, evaluate

DEST = OUT.with_name('effibench_main_analysis.json')
REPORT = OUT.with_name('effibench_main_report.md')


def main():
    records = [json.loads(line) for line in OUT.open(encoding='utf-8')]
    ids = {r['problem_idx'] for r in records if r['status'] == 'complete'}
    tasks = {r['problem_idx']: r for line in DATA.open(encoding='utf-8')
             if (r := json.loads(line))['problem_idx'] in ids}
    analyzed = json.loads(DEST.read_text(encoding='utf-8')) if DEST.exists() else []
    finished = {item['problem_idx'] for item in analyzed}
    for record in records:
        if record['status'] != 'complete' or record['problem_idx'] in finished:
            continue
        row = tasks[record['problem_idx']]
        base_code = record.get('repair_raw', record['base_raw'])
        initial = record.get('repair_check', record['base_check'])
        base_ms = initial['total_ns'] / 1e6
        repeats = max(1, min(500, round(200 / max(base_ms, 0.05))))
        item = {'problem_idx': record['problem_idx'], 'task_name': record['task_name'],
                'repeats': repeats}
        for label, code in [('base', base_code)] + [(m, record[m + '_raw']) for m in ('vanilla', 'perfcodegen', 'ours')]:
            runs = [evaluate(code, row, timeout=25, repeats=repeats) for _ in range(3)]
            item[label] = {
                'correct': all(x.get('correct') for x in runs),
                'total_ms': [round(x['total_ns'] / 1e6, 3) for x in runs if x.get('correct')],
                'error': next((x.get('error') or x.get('failed_test') for x in runs if not x.get('correct')), None),
            }
        if item['base']['correct']:
            baseline = statistics.median(item['base']['total_ms'])
            for label in ('vanilla', 'perfcodegen', 'ours'):
                data = item[label]
                data['speedup'] = (round(baseline / statistics.median(data['total_ms']), 3)
                                   if data['correct'] and statistics.median(data['total_ms']) > 0 else None)
        analyzed.append(item)
        DEST.write_text(json.dumps(analyzed, ensure_ascii=False, indent=2), encoding='utf-8')
        print('remeasured', item['problem_idx'], flush=True)
    lines = ['# EffiBench 主实验首轮结果', '',
             f'- 冻结题目：30；完成三组对照：{len(analyzed)}；正确性阶段失败：'
             f"{sum(r['status'] == 'base_wrong' for r in records)}；基准解判题失败："
             f"{sum(r['status'] == 'canonical_failed' for r in records)}。",
             '- 同一题四组共用初始正确代码；每组在相同重复次数下运行完整测试集三次，表中基线时间为三次中位数。',
             f"- 未放大前，单次测试集的基线耗时中位数约为 {statistics.median((r.get('repair_check') or r['base_check'])['total_ns'] / 1e6 for r in records if r['status'] == 'complete'):.3f} ms；因此放大计时是必要的。",
             '- 目前只用数据集自带测试核验正确性，尚缺独立保留测试；极大加速比须复核算法与额外输入。',
             '- 本轮为首轮探索，不能据此断言增强反馈稳定优于 PerfCodeGen。', '']
    for label in ('vanilla', 'perfcodegen', 'ours'):
        valid = [x[label] for x in analyzed if x['base']['correct']]
        sps = [x['speedup'] for x in valid if x['speedup'] is not None]
        lines.append(f"- {label}: 复测正确 {sum(x['correct'] for x in valid)}/{len(valid)}；"
                     f"中位加速比 {statistics.median(sps):.2f}x；≥1.3x 的题 {sum(x >= 1.3 for x in sps)}。")
    lines += ['', '| 题号 | 基线 ms | 普通优化 | PerfCodeGen | 增强反馈 |',
              '|---|---:|---:|---:|---:|']
    for item in analyzed:
        def cell(label):
            d = item[label]
            return f"{d['speedup']:.2f}x" if d.get('speedup') is not None else '错误/超时'
        base = statistics.median(item['base']['total_ms']) if item['base']['total_ms'] else None
        lines.append(f"| {item['problem_idx']} | {base:.2f} | {cell('vanilla')} | "
                     f"{cell('perfcodegen')} | {cell('ours')} |" if base is not None else
                     f"| {item['problem_idx']} | 失败 | — | — | — |")
    lines += ['', '## 需进一步核验的案例', '',
              '- 860：三组加速比都接近 1，适合检查初始实现是否已接近最优。',
              '- 1353：PerfCodeGen 组约 0.60x，需检查瓶颈反馈是否诱导了较慢算法。',
              '- 809：PerfCodeGen 组约 93.67x；代码采用双指针并提前退出，但仍需独立输入验证通用正确性。',
              '- 多题优化版未通过原测试；这些候选的性能不计入加速比。',
              '- 原始单次测试集耗时偏短，放大后才能讨论计时差异。']
    REPORT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', REPORT, flush=True)


if __name__ == '__main__':
    main()
