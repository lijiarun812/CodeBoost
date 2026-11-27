#!/usr/bin/env python3
"""输入规模升档实验：确认「空间」到底能不能测出来。

背景
----
EffiBench 原生用例太小：40 题里 `peak_rss − 基线 ≥ 5 MB` 的可分辨题数 **0**，
`peak_tracemalloc` 中位 0.0017 MB。于是「Ours 省了内存」这件事没有第二个维度可证。
本脚本把**单条用例的输入规模**按目标长度放大（1e3 / 1e4 / 1e5），看信号能不能长出来。

三条纪律（违反就没意义）
----
1. **只测资源、不判对错**：放大后答案必变，`eval/inputs.scaled_calls()` 已经把
   `assert f(x) == y` 改成裸调用 `f(x)`；
2. **只进 diagnostic_only**：工作时长变了，**时间数字与主结果不可比**，本脚本的时间列仅参考；
3. **一档上限 MAX_SEQ**：单参数最长 20 万，防 O(n²) 题把机器打爆（跑不完记为 timeout，本身也是结论）。

用法
----
    # 8 题，输入规模 1e3 / 1e4 / 1e5 三档 + 原始规模对照
    python tools/probe_input_scaling.py --num 8 --start 100 --sizes 0,1000,10000,100000

    # 结果落盘（JSONL），并在 stdout 打印「升档倍数 × 信号强度」曲线
    python tools/probe_input_scaling.py --num 20 --sizes 0,1000,10000,100000 --out results/scaling_probe.jsonl

    --sizes 里的 0 表示「原始规模」（只把 assert 改成裸调用，不放大），作为对照基线。

输出
----
stdout：逐题逐档一行 + 每档汇总（`≥5 MB` 命中数/占比、`peak_tm` 中位、超时数）
JSONL ：每题一档一行，字段见 `measure_one()`
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import statistics as st
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import harness, inputs  # noqa: E402
from eval.dual_report import measure_rss_baseline  # noqa: E402


def base_literal_len(case_src: str) -> int:
    """测试块里最大的字符串/列表字面量长度——用作「原始输入规模」的度量。"""
    try:
        tree = ast.parse(case_src)
    except SyntaxError:
        return 0
    best = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            best = max(best, len(node.value))
        elif isinstance(node, (ast.List, ast.Tuple)):
            best = max(best, len(node.elts))
    return best


def factor_for(case_src: str, target: int) -> int:
    """把「目标输入规模」折算成放大倍数；0 表示原始规模。"""
    if target <= 0:
        return 1
    base = base_literal_len(case_src)
    if base <= 0:
        return 1
    return max(1, int(math.ceil(target / float(base))))


def measure_one(entry, size, baseline, e_reps, max_reps, stale_s):
    tier = inputs.scale_tier({"test": entry["test_case"]}, factor_for(entry["test_case"], size))
    row = {"problem_idx": entry["problem_idx"], "task_name": entry["task_name"],
           "size": size, "factor": tier["factor"], "n_calls": tier["n_calls"],
           "note": tier["note"], "base_len": base_literal_len(entry["test_case"])}
    if tier["note"] != "ok":
        row["status"] = tier["note"]
        return row
    m = harness.measure_code(
        entry["canonical_solution"], tier["test"], kind="test_block", entry="solution",
        e_reps=e_reps, target_batch_s=0.01, max_reps=max_reps, stale_s=stale_s,
    )
    row["status"] = m.get("status")
    if row["status"] != "ok":
        row["error"] = str(m.get("error"))[:160]
        return row
    mem = m.get("mem") or {}
    peak_tm = mem.get("peak_tracemalloc_mb") or 0.0
    peak_rss = mem.get("peak_rss_mb") or 0.0
    row.update({
        "peak_tm_mb": round(peak_tm, 4),
        "peak_rss_mb": round(peak_rss, 2),
        "rss_delta_mb": round(max(0.0, peak_rss - (baseline or 0.0)), 2),
        "ms": round(m.get("mean_trimmed_ms") or 0.0, 3),
        "distinguishable": max(0.0, peak_rss - (baseline or 0.0)) >= 5.0,
    })
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(REPO_ROOT / "datasets" / "dataset.json"))
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--num", type=int, default=8)
    ap.add_argument("--sizes", default="0,1000,10000,100000",
                    help="逗号分隔的目标输入规模；0 = 原始规模")
    ap.add_argument("--e-reps", type=int, default=1, help="批次数（这里只关心内存，1 就够）")
    ap.add_argument("--max-reps", type=int, default=1)
    ap.add_argument("--stale-s", type=float, default=15.0)
    ap.add_argument("--rss-baseline", type=float, default=None)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    baseline = args.rss_baseline
    if baseline is None:
        cache = REPO_ROOT / "results" / "rss_baseline.json"
        if cache.exists():
            try:
                baseline = json.loads(cache.read_text(encoding="utf-8")).get("peak_rss_mb")
            except ValueError:
                baseline = None
        if baseline is None:
            baseline = measure_rss_baseline()
    print("空载 RSS 基线 = %s MB" % ("%.2f" % baseline if baseline else "未知"))

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    picked = [e for e in data
              if "class Solution" in (e.get("canonical_solution") or "")
              and e.get("markdown_description") and e.get("test_case")]
    entries = picked[args.start:args.start + args.num]
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    rows = []
    fh = Path(args.out).open("w", encoding="utf-8") if args.out else None
    try:
        for e in entries:
            for size in sizes:
                row = measure_one(e, size, baseline, args.e_reps, args.max_reps, args.stale_s)
                rows.append(row)
                if fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                if row.get("status") == "ok":
                    print("%5s %-26s size=%-7s x%-5s tm=%9.4f rss=%6.1f delta=%6.1f ms=%8.3f %s"
                          % (row["problem_idx"], row["task_name"][:26], size, row["factor"],
                             row["peak_tm_mb"], row["peak_rss_mb"], row["rss_delta_mb"],
                             row["ms"], "✓" if row["distinguishable"] else ""), flush=True)
                else:
                    print("%5s %-26s size=%-7s x%-5s %s %s"
                          % (row["problem_idx"], row["task_name"][:26], size, row["factor"],
                             row["status"], row.get("error", "")[:40]), flush=True)
    finally:
        if fh:
            fh.close()

    print("\n" + "=" * 74)
    print("升档倍数 × 信号强度（阈值：peak_rss − 基线 ≥ 5 MB 视为可分辨）")
    print("%-8s %-6s %-10s %-12s %-12s %-8s" % ("目标规模", "有效", "可分辨", "占比", "peak_tm中位", "超时"))
    for size in sizes:
        grp = [r for r in rows if r["size"] == size]
        ok = [r for r in grp if r.get("status") == "ok"]
        hit = [r for r in ok if r.get("distinguishable")]
        to = [r for r in grp if r.get("status") == "timeout"]
        med = st.median([r["peak_tm_mb"] for r in ok]) if ok else 0.0
        print("%-8s %-6d %-10d %-12s %-12s %-8d"
              % (size, len(ok), len(hit),
                 "%.0f%%" % (100.0 * len(hit) / len(ok)) if ok else "n/a",
                 "%.4f MB" % med, len(to)))
    print("=" * 74)
    print("注意：本表**只用于空间诊断**；放大输入后工作时长变了，时间列不可与主结果比较。")
    if args.out:
        print("明细已写入 %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
