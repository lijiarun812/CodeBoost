"""Repeated paired timing and progressive-feedback effect analysis."""
import json
import statistics
from pathlib import Path

from analyze_effibench_v2 import measure
from run_effibench_main import ROOT
from run_feedback_g import GROUPS, OUT as GENERATIONS, SOURCE, FEEDBACK

DEST = ROOT / 'experiment/results/g_analysis.json'
REPORT = ROOT / 'experiment/results/g_report.md'


def save(data):
    tmp = DEST.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(DEST)


def speed(base, candidate):
    if not (base.get('correct') and candidate.get('correct') and candidate.get('median_ms')):
        return None
    return round(base['median_ms'] / candidate['median_ms'], 3)


def main():
    generated = json.loads(GENERATIONS.read_text(encoding='utf-8'))
    feedback = json.loads(FEEDBACK.read_text(encoding='utf-8'))
    ids = {int(k) for k in generated}
    source = {r['problem_idx']: r for r in json.loads(SOURCE.read_text(encoding='utf-8'))
              if r['problem_idx'] in ids}
    state = json.loads(DEST.read_text(encoding='utf-8')) if DEST.exists() else {}
    for key, record in generated.items():
        if record.get('status') != 'complete':
            continue
        row = source[int(key)]
        base_code = row['completion']
        original_repeats = max(1, min(300, round(150_000_000 / max(feedback[key]['total_ns'], 100_000))))
        holdout_ns = max(1, len(record['holdout_test'].splitlines()) * feedback[key]['total_ns'] /
                         len(row['test_case'].splitlines()))
        holdout_repeats = max(1, min(300, round(150_000_000 / max(holdout_ns, 100_000))))
        item = state.setdefault(key, {'problem_idx': int(key), 'group': record['group'],
                                      'original_repeats': original_repeats,
                                      'holdout_repeats': holdout_repeats, 'candidates': {g: {} for g in GROUPS}})
        if 'base' not in item:
            item['base'] = {
                'original': measure(base_code, row['test_case'], original_repeats),
                'holdout': measure(base_code, record['holdout_test'], holdout_repeats),
            }
            save(state)
        for group in GROUPS:
            for replicate, candidate in record['generations'][group].items():
                if replicate in item['candidates'][group]:
                    continue
                result = {'correct': candidate['original_check'].get('correct', False) and
                                     candidate['holdout_check'].get('correct', False)}
                if result['correct']:
                    result['original'] = measure(candidate['code'], row['test_case'], original_repeats)
                    result['holdout'] = measure(candidate['code'], record['holdout_test'], holdout_repeats)
                    result['correct'] = result['original']['correct'] and result['holdout']['correct']
                if result['correct']:
                    result['original_speedup'] = speed(item['base']['original'], result['original'])
                    result['holdout_speedup'] = speed(item['base']['holdout'], result['holdout'])
                    result['success'] = bool(result['original_speedup'] and result['holdout_speedup'] and
                                             result['original_speedup'] >= 1.0 and result['holdout_speedup'] >= 1.3)
                else:
                    result['success'] = False
                item['candidates'][group][replicate] = result
                save(state)
                print('timed', key, group, replicate, flush=True)
    lines = ['# G0-G3 递进反馈实验', '',
             f'- 完成题目：{len(state)}；每题每组 2 次生成，四组共 {len(state) * 8} 次；'
             '正确性失败按未优化成功计入分母。',
             '- 成功标准：原测试仍正确且不变慢，额外测试仍正确且加速至少 1.3 倍。',
             '- 速度由相同测试集、相同重复次数、各三次计时的中位数成对计算。', '']
    for subset, title in [('main', '主组'), ('short_control', '短任务对照')]:
        keys = [k for k, v in state.items() if v['group'] == subset]
        lines += [f'## {title}（{len(keys)} 题，{len(keys) * 2} 组内重复）', '',
                  '| 组 | 两套测试正确 | 成功优化 | 正确候选的额外测试中位加速 | 提示 Token 中位数 | API 延迟中位数 |',
                  '|---|---:|---:|---:|---:|---:|']
        for group in GROUPS:
            outcomes = [state[k]['candidates'][group][rep] for k in keys for rep in ('1', '2')]
            apis = [generated[k]['generations'][group][rep]['api'] for k in keys for rep in ('1', '2')]
            speedups = [x['holdout_speedup'] for x in outcomes if x['correct'] and x.get('holdout_speedup')]
            median_speed = f'{statistics.median(speedups):.2f}x' if speedups else '—'
            lines.append(f"| {group} | {sum(x['correct'] for x in outcomes)}/{len(outcomes)} | "
                         f"{sum(x['success'] for x in outcomes)}/{len(outcomes)} | {median_speed} | "
                         f"{statistics.median(x['prompt_tokens'] for x in apis):.0f} | "
                         f"{statistics.median(x['latency_s'] for x in apis):.2f}s |")
        lines.append('')
        for lower, upper in zip(GROUPS, GROUPS[1:]):
            pairs = [(state[k]['candidates'][lower][rep], state[k]['candidates'][upper][rep])
                     for k in keys for rep in ('1', '2')]
            delta = sum(int(b['success']) - int(a['success']) for a, b in pairs)
            valid = [(a, b) for a, b in pairs if a['correct'] and b['correct'] and
                     a.get('holdout_speedup') and b.get('holdout_speedup')]
            ratios = [b['holdout_speedup'] / a['holdout_speedup'] for a, b in valid]
            ratio_text = f'{statistics.median(ratios):.2f}' if ratios else '—'
            lines.append(f'- {lower}→{upper}: 成功数净变化 {delta:+d}/{len(pairs)}；'
                         f'两者都正确的 {len(valid)} 对中，后组/前组加速比中位数 {ratio_text}。')
        lines.append('')
    usable = [feedback[k] for k in state]
    valid_candidates = [v for item in state.values() for group in GROUPS
                        for v in item['candidates'][group].values() if v['correct']]
    original_spread = [(max(v['original']['ms']) - min(v['original']['ms'])) /
                       statistics.median(v['original']['ms']) for v in valid_candidates
                       if len(v['original']['ms']) == 3 and statistics.median(v['original']['ms']) > 0]
    holdout_spread = [(max(v['holdout']['ms']) - min(v['holdout']['ms'])) /
                      statistics.median(v['holdout']['ms']) for v in valid_candidates
                      if len(v['holdout']['ms']) == 3 and statistics.median(v['holdout']['ms']) > 0]
    lines += ['## 反馈成本与限制', '',
              f"- G1 计时采集中位耗时 {statistics.median(x['timing_collection_s'] for x in usable):.3f}s；"
              'G2 复用该次测量得到的最慢测试，增量采集成本接近零；'
              f"G3 热点剖析额外中位耗时 {statistics.median(x['profile_collection_s'] for x in usable):.3f}s。",
              '- 提示 Token 与 API 延迟见上表；完整用量及原始计时在 JSON 结果中。',
              f'- 三次计时的极差/中位数：原测试中位 {statistics.median(original_spread):.1%}，'
              f'额外测试中位 {statistics.median(holdout_spread):.1%}；'
              f'超过 20% 的候选分别为 {sum(x > .2 for x in original_spread)}/{len(original_spread)} 和 '
              f'{sum(x > .2 for x in holdout_spread)}/{len(holdout_spread)}。',
              '- 热点耗时是跟踪器的近似值，不能与普通计时的绝对值直接比较。',
              '- 额外测试来自同一基准的不同随机种子；题目数量及每组仅两次生成限制了统计推断。']
    REPORT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', REPORT, flush=True)


if __name__ == '__main__':
    main()
