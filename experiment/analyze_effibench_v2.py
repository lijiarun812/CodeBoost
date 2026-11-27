"""Paired repeated timing on original and independent tests, with checkpointing."""
import json
import statistics
from pathlib import Path

from run_effibench_main import ROOT, evaluate
from run_effibench_v2 import SOURCE, OUT, METHODS

DEST = ROOT / 'experiment/results/effibench_v2_analysis.json'
REPORT = ROOT / 'experiment/results/effibench_v2_report.md'


def measure(code, test_case, repeats):
    runs = [evaluate(code, {'test_case': test_case}, timeout=25, repeats=repeats) for _ in range(3)]
    times = [x['total_ns'] / 1e6 for x in runs if x.get('correct')]
    return {'correct': len(times) == 3, 'ms': [round(t, 3) for t in times],
            'median_ms': round(statistics.median(times), 3) if len(times) == 3 else None,
            'error': next((x.get('error') or x.get('failed_test') for x in runs if not x.get('correct')), None)}


def main():
    source = json.loads(SOURCE.read_text(encoding='utf-8'))
    records = json.loads(OUT.read_text(encoding='utf-8'))
    analyzed = json.loads(DEST.read_text(encoding='utf-8')) if DEST.exists() else {}
    for key, rec in records.items():
        if rec.get('status') != 'complete' or key in analyzed:
            continue
        row = next(x for x in source if x['problem_idx'] == rec['problem_idx'])
        base = row['completion']
        repeat_original = max(1, min(500, round(150_000_000 / max(rec['base_check']['total_ns'], 100_000))))
        repeat_holdout = max(1, min(500, round(150_000_000 / max(rec['holdout_base']['total_ns'], 100_000))))
        item = {'problem_idx': rec['problem_idx'], 'group': rec['group'],
                'repeat_original': repeat_original, 'repeat_holdout': repeat_holdout,
                'methods': {}}
        for label, code in [('base', base)] + [(m, rec[m]['raw']) for m in METHODS]:
            if label != 'base' and not (rec[label]['original_check'].get('correct') and
                                        rec[label]['holdout_check'].get('correct')):
                item['methods'][label] = {'correct': False}
                continue
            original = measure(code, row['test_case'], repeat_original)
            holdout = measure(code, rec['holdout_test'], repeat_holdout)
            item['methods'][label] = {'correct': original['correct'] and holdout['correct'],
                                      'original': original, 'holdout': holdout}
        if item['methods']['base']['correct']:
            for label in METHODS:
                m = item['methods'][label]
                if m['correct']:
                    for suite in ('original', 'holdout'):
                        m[suite]['speedup'] = round(item['methods']['base'][suite]['median_ms'] /
                                                     m[suite]['median_ms'], 3) if m[suite]['median_ms'] else None
        analyzed[key] = item
        DEST.write_text(json.dumps(analyzed, ensure_ascii=False, indent=2), encoding='utf-8')
        print('timed', key, flush=True)
    lines = ['# EffiBench 第二轮实验结果', '',
             f"- 冻结候选：{len(records)}；预检有效并完成四组生成："
             f"{sum(x.get('status') == 'complete' for x in records.values())}；完成重复计时：{len(analyzed)}。",
             '- 四组共用公开 GPT-3.5 正确初始代码，模型为 qwen4-max；每组在原测试和额外测试上分别重复三次。',
             '- 主组基线原测试耗时至少 5 ms；短任务对照单独列出。', '']
    for group in ('main', 'short_control'):
        items = [x for x in analyzed.values() if x['group'] == group and x['methods']['base']['correct']]
        lines.append(f'## {"主组" if group == "main" else "短任务对照"}（{len(items)} 题）')
        lines.append('')
        for label in METHODS:
            valid = [x['methods'][label] for x in items if x['methods'][label]['correct']]
            sps = [x['holdout']['speedup'] for x in valid if x['holdout'].get('speedup')]
            median = f'{statistics.median(sps):.2f}x' if sps else '—'
            success = sum(x['holdout']['speedup'] >= 1.3 and x['original']['speedup'] >= 1.0
                          for x in valid if x['holdout'].get('speedup') and x['original'].get('speedup'))
            lines.append(f'- {label}: 两套测试均正确 {len(valid)}/{len(items)}；额外测试中位加速比 {median}；'
                         f'两套测试一致的 ≥1.3x 改进 {success} 题。')
        paired = [x for x in items if x['methods']['ours']['correct'] and x['methods']['perfcodegen']['correct']]
        if paired:
            ratios = [x['methods']['ours']['holdout']['speedup'] /
                      x['methods']['perfcodegen']['holdout']['speedup'] for x in paired]
            lines.append(f'- 增强反馈对 PerfCodeGen 的成对比较：{len(paired)} 题；'
                         f'增强反馈较快 {sum(v > 1 for v in ratios)} 题；成对速度比中位数 {statistics.median(ratios):.2f}。')
        lines.append('')
    lines += ['## 限制', '',
              '- 额外测试来自官方生成器的不同随机种子，仍属于同一数据来源；尚需更大输入的人工核验。',
              '- 题量和一次生成的随机波动限制了结论强度。极端加速须逐题检查代码与输入。',
              '- 详细原始时间、超时及正确性见 `effibench_v2_analysis.json` 和 `effibench_v2_results.json`。']
    REPORT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', REPORT, flush=True)


if __name__ == '__main__':
    main()
