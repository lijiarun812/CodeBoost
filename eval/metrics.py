"""PerfCodeGen / EffiLearner 共用的效率指标（论文口径）。

指标定义（与官方 evaluate_prompt.evaluate_execution_time 对齐）：
- %Correct : 可执行且通过全部用例的采样比例
- Speedup  : mean(min_gt_time / candidate_time)，只在正确采样上统计
- %Opt@k   : 前 k 个采样中最好的一次比 ground truth 快 10% 以上的题目比例
- Best@k   : 前 k 个采样中最快那次的 speedup
- %Opt/Speedup 的 “avg” 口径：先在题内对采样求平均，再跨题求平均
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

KS = (1, 5, 10)
OPT_THRESHOLD = 0.10


def speedup(gt_ms, cand_ms):
    """gt 越慢、cand 越快，speedup 越大；cand 非法返回 0。"""
    if not cand_ms or cand_ms <= 0 or not gt_ms:
        return 0.0
    return gt_ms / cand_ms


def is_opt(gt_ms, cand_ms, threshold=OPT_THRESHOLD):
    """比 ground truth 快超过 threshold（默认 10%）算一次优化。"""
    if not gt_ms or not cand_ms or cand_ms <= 0:
        return False
    return (gt_ms - cand_ms) / gt_ms > threshold


def best_at_k(samples, k):
    """前 k 个采样里最快的耗时；没有可用采样返回 None。"""
    usable = [s["ms"] for s in samples[:k] if s.get("correct") and s.get("ms")]
    return min(usable) if usable else None


EXTREME_SPEEDUP = 100.0


def _filter_samples(task, max_speedup):
    """丢掉“比 ground truth 快 100 倍以上”的采样。

    这是官方 PerfCodeGen 的做法（`evaluate_prompt.py` 对 HumanEval/MBPP 有
    `if min_groundtruth_time / exec_time > 100: continue`）：HumanEval 的
    canonical solution 往往写得很朴素（例如 is_prime 用 range(2, n-1)），模型随手
    写成 sqrt 版本就能“快 100 倍”，那是 GT 太慢，不是我们的优化有效。
    不过滤的话 speedup 会出现几十上百倍，整张表就废了。
    """
    kept, dropped = [], 0
    for s in task.get("samples") or []:
        if max_speedup and s.get("correct") and s.get("ms"):
            if speedup(task["gt_ms"], s["ms"]) > max_speedup:
                dropped += 1
                continue
        kept.append(s)
    return kept, dropped


def aggregate(tasks, max_speedup=EXTREME_SPEEDUP):
    """把逐题记录汇总成可上表的指标。

    tasks: [{"task_id", "gt_ms", "samples": [{"correct", "ms"}], "final": {"correct", "ms"}}]
    max_speedup: 超过该倍数的采样视为测量伪影（官方 100，传 0 关闭）
    """
    n_tasks = len(tasks)
    prepared, n_dropped = [], 0
    for t in tasks:
        kept, dropped = _filter_samples(t, max_speedup)
        n_dropped += dropped
        prepared.append((t, kept))

    total_samples = sum(len(kept) for _, kept in prepared)
    correct_samples = sum(1 for _, kept in prepared for s in kept if s.get("correct"))
    executed = [(t, kept) for t, kept in prepared
                if any(s.get("correct") for s in kept)]

    per_task_speedup = []
    per_task_opt = []
    for t, kept in executed:
        su = [speedup(t["gt_ms"], s["ms"]) for s in kept if s.get("correct") and s.get("ms")]
        if su:
            per_task_speedup.append(statistics.mean(su))
            per_task_opt.append(1 if is_opt(t["gt_ms"], min(
                s["ms"] for s in kept if s.get("correct") and s.get("ms"))) else 0)

    out = {
        "n_tasks": n_tasks,
        "n_executed": len(executed),
        "n_samples": total_samples,
        "pct_correct": round(correct_samples / total_samples, 4) if total_samples else 0.0,
        "speedup_avg": round(statistics.mean(per_task_speedup), 4) if per_task_speedup else 0.0,
        "pct_opt_avg": round(statistics.mean(per_task_opt), 4) if per_task_opt else 0.0,
    }
    if max_speedup:
        out["max_speedup"] = max_speedup
        out["n_dropped_extreme"] = n_dropped
    for k in KS:
        best_su, opt_flags = [], []
        for t, kept in prepared:
            b = best_at_k(kept, k)
            if not b:
                continue
            best_su.append(speedup(t["gt_ms"], b))
            opt_flags.append(1 if is_opt(t["gt_ms"], b) else 0)
        out["pct_opt@%d" % k] = round(sum(opt_flags) / n_tasks, 4) if n_tasks else 0.0
        out["speedup@%d" % k] = round(statistics.mean(best_su), 4) if best_su else 0.0

    finals = [t["final"] for t, _ in prepared
              if (t.get("final") or {}).get("correct") and t["final"].get("ms")]
    if finals:
        out["final_pct_correct"] = round(len(finals) / n_tasks, 4)
        out["final_speedup_avg"] = round(
            statistics.mean([speedup(t["gt_ms"], t["final"]["ms"]) for t, _ in prepared
                             if (t.get("final") or {}).get("correct") and t["final"].get("ms")]), 4)
        out["final_pct_opt"] = round(
            statistics.mean([1 if is_opt(t["gt_ms"], t["final"]["ms"]) else 0 for t, _ in prepared
                             if (t.get("final") or {}).get("correct") and t["final"].get("ms")]), 4)

    # “我们的优化到底有没有用”要看 final 相对 base 的变化，而不是相对 GT
    # （HumanEval 的 GT 常常很朴素，base 一开始就已经比 GT 快）
    gains = []
    for t, kept in prepared:
        fin = t.get("final") or {}
        base_ms = t.get("base_ms") or (best_at_k(kept, len(kept)) if kept else None)
        if base_ms and fin.get("correct") and fin.get("ms"):
            gains.append((base_ms, fin["ms"]))
    if gains:
        out["n_vs_base"] = len(gains)
        out["speedup_vs_base_avg"] = round(
            statistics.mean([b / f for b, f in gains if f]), 4)
        out["pct_opt_vs_base"] = round(
            statistics.mean([1 if (b - f) / b > OPT_THRESHOLD else 0 for b, f in gains]), 4)

    mads = [t.get("rel_mad") for t in tasks if t.get("rel_mad")]
    if mads:
        out["median_rel_mad"] = round(statistics.median(mads), 4)
    return out


def format_summary_table(summary, title="复现结果"):
    lines = [
        "### %s" % title,
        "",
        "| 指标 | 值 |",
        "|---|---|",
    ]
    order = [
        ("n_tasks", "题目数"),
        ("n_executed", "至少一次正确的题数"),
        ("n_samples", "采样总数"),
        ("pct_correct", "%Correct"),
        ("speedup_avg", "Speedup(avg)"),
        ("pct_opt_avg", "%Opt(avg)"),
        ("speedup@1", "Speedup@1"),
        ("speedup@5", "Speedup@5"),
        ("speedup@10", "Speedup@10"),
        ("pct_opt@1", "%Opt@1"),
        ("pct_opt@5", "%Opt@5"),
        ("pct_opt@10", "%Opt@10"),
        ("final_speedup_avg", "最终版 Speedup"),
        ("final_pct_opt", "最终版 %Opt"),
        ("n_vs_base", "相对 base 可比的题数"),
        ("speedup_vs_base_avg", "最终版 Speedup（vs base）"),
        ("pct_opt_vs_base", "最终版 %Opt（vs base）"),
        ("median_rel_mad", "测量相对 MAD 中位数"),
        ("n_dropped_extreme", "剔除的极端加速比采样（>max_speedup）"),
    ]
    for key, label in order:
        if key in summary:
            lines.append("| %s | %s |" % (label, summary[key]))
    return "\n".join(lines)


def load_summary(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
