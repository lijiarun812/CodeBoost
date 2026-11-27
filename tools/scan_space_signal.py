#!/usr/bin/env python3
"""空间信号扫描：给题库里的每道题量一次，输出「这题能不能分辨空间优化」。

为什么需要它：EffiBench 原生用例普遍很小，`peak_tracemalloc` 常在亚 KB 级、
baseline 与 final 完全相同，空间列全 0（实测 24 题中位 0.0017 MB）。
不先筛题，任何「空间优化 X%」都无从谈起。

用它做什么：B 冻结题库时按本脚本的输出筛「空间可分辨」的题；
组长跑完矩阵后，空间列只在这个子集上出数，并注明是**定向筛选子集**。

测量一律走 `eval/harness.py`（唯一尺子），本脚本不含任何自己的计时逻辑。

用法
----
    # 全量扫描（约 1000 题；每题 2 趟测量：计时 + 内存）
    python tools/scan_space_signal.py --dataset datasets/dataset.json --out results/bank_scan.jsonl

    # 先扫 40 题看通过率，再决定要不要全量
    python tools/scan_space_signal.py --num 40

    # 断点续跑：已测过的题自动跳过（读 --out 里已有的 JSONL）
    python tools/scan_space_signal.py --out results/bank_scan.jsonl

参数
----
    --dataset        EffiBench 风格 JSON（数组，含 canonical_solution / test_case / markdown_description）
    --start / --num  列表切片（**不是** problem_idx），默认 0 / 全部
    --kind           测试块类型，EffiBench 用 test_block（默认）
    --entry          入口名，默认 solution（harness 会把它解析成实例）
    --peak-tm-min    H1 空间门槛（MB），默认 0.05
    --rss-delta-min  H2 MU 门槛（MB），默认 5.0
    --min-ms / --max-cv  H3' 时间门槛，默认 1.0 ms / 0.10
    --rss-baseline   空载 RSS 基线（MB）；缺省读 results/rss_baseline.json，没有就用 harness 量一次
    --out            逐题结果写 JSONL（每行一题，边跑边写，可续跑）；缺省只打印汇总
    --quiet          只打汇总

输出（JSONL，每行）
----
    {"problem_idx": 3, "task_name": "...", "status": "ok",
     "peak_tm_mb": 0.001, "peak_rss_mb": 21.4, "rss_delta_mb": 0.9,
     "baseline_ms": 0.0767, "cv": 0.0595, "reps": 13736,
     "pass_h1_space": false, "pass_h2_mu": false, "pass_h3_time": false,
     "protocol_version": "proto-2026.11", "measured_at": "..."}

    status != "ok" 的题只写 status/error，不算入任何通过率。

汇总（stdout）
----
    各门槛单独通过率 + H1∩H3' 联合通过率 + 按 30 题/全量区分「试测外推」与「实测通过率」。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import statistics as st
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import harness  # noqa: E402
from eval.dual_report import measure_rss_baseline  # noqa: E402


def load_entries(path: Path, start: int, num: int | None):
    data = json.loads(path.read_text(encoding="utf-8"))
    picked = [e for e in data
              if "class Solution" in (e.get("canonical_solution") or "")
              and e.get("markdown_description") and e.get("test_case")]
    end = None if not num else start + num
    return picked[start:end]


def already_done(out: Path) -> set:
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            done.add(row.get("problem_idx"))
    return done


def grade(entry, args, baseline):
    m = harness.measure_code(
        entry["canonical_solution"], entry["test_case"],
        kind=args.kind, entry=args.entry,
        e_reps=harness.E_REPS, target_batch_s=args.target_batch_s,
        max_reps=args.max_reps,
    )
    if m.get("status") != "ok":
        return {"problem_idx": entry["problem_idx"], "task_name": entry["task_name"],
                "status": m.get("status"), "error": str(m.get("error"))[:200]}
    mem = m.get("mem") or {}
    peak_tm = mem.get("peak_tracemalloc_mb") or 0.0
    peak_rss = mem.get("peak_rss_mb") or 0.0
    base_ms = m.get("mean_trimmed_ms") or 0.0
    cv = (m.get("stats") or {}).get("cv") or 1.0
    delta = max(0.0, peak_rss - (baseline or 0.0))
    return {
        "problem_idx": entry["problem_idx"],
        "task_name": entry["task_name"],
        "status": "ok",
        "peak_tm_mb": round(peak_tm, 6),
        "peak_rss_mb": round(peak_rss, 2),
        "rss_delta_mb": round(delta, 2),
        "baseline_ms": round(base_ms, 4),
        "cv": round(cv, 4),
        "reps": m.get("reps"),
        "pass_h1_space": peak_tm >= args.peak_tm_min,
        "pass_h2_mu": delta >= args.rss_delta_min,
        "pass_h3_time": base_ms >= args.min_ms and cv <= args.max_cv,
        "protocol_version": harness.PROTOCOL_VERSION,
        "measured_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }


def summarize(rows, args, baseline) -> str:
    ok = [r for r in rows if r.get("status") == "ok"]
    bad = [r for r in rows if r.get("status") != "ok"]
    n = len(ok)
    out = []
    w = out.append
    w("=" * 68)
    w("空间信号扫描：有效 %d 题，失败 %d 题（失败不计入通过率）" % (n, len(bad)))
    w("  空载 RSS 基线 = %s MB" % ("%.2f" % baseline if baseline else "未知（H2 不可用）"))
    if not n:
        w("没有有效样本。")
        return "\n".join(out)
    pct = lambda k: 100.0 * sum(1 for r in ok if r.get(k)) / n
    w("  H1 空间 (peak_tm >= %.3f MB)         : %5.1f%%  (%d/%d)"
      % (args.peak_tm_min, pct("pass_h1_space"), sum(1 for r in ok if r["pass_h1_space"]), n))
    w("  H2 MU   (rss-基线 >= %.1f MB)        : %5.1f%%  (%d/%d，只记录不作门槛)"
      % (args.rss_delta_min, pct("pass_h2_mu"), sum(1 for r in ok if r["pass_h2_mu"]), n))
    w("  H3' 时间(baseline_ms >= %.1f 且 cv<=%.2f): %5.1f%%  (%d/%d)"
      % (args.min_ms, args.max_cv, pct("pass_h3_time"), sum(1 for r in ok if r["pass_h3_time"]), n))
    both = sum(1 for r in ok if r["pass_h1_space"] and r["pass_h3_time"])
    w("  H1 ∩ H3'（推荐筛题口径）              : %5.1f%%  (%d/%d)" % (100.0 * both / n, both, n))
    tms = [r["peak_tm_mb"] for r in ok]
    mss = [r["baseline_ms"] for r in ok]
    w("  peak_tm 中位 %.4f MB / 最大 %.4f MB；baseline_ms 中位 %.4f / 最大 %.2f"
      % (st.median(tms), max(tms), st.median(mss), max(mss)))
    if n < 200:
        w("  ⚠️ 样本 %d 题：以上是**试测**通过率，不能直接外推成 1000 题的产出，"
          "请在更广样本上校准后再定冻结口径。" % n)
    else:
        w("  样本 %d 题：可作为冻结题库的实测通过率。" % n)
    w("=" * 68)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(REPO_ROOT / "datasets" / "dataset.json"))
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--num", type=int, default=None)
    ap.add_argument("--kind", default="test_block")
    ap.add_argument("--entry", default="solution")
    ap.add_argument("--peak-tm-min", type=float, default=0.05)
    ap.add_argument("--rss-delta-min", type=float, default=5.0)
    ap.add_argument("--min-ms", type=float, default=1.0)
    ap.add_argument("--max-cv", type=float, default=0.10)
    ap.add_argument("--rss-baseline", type=float, default=None)
    ap.add_argument("--target-batch-s", type=float, default=0.15)
    ap.add_argument("--max-reps", type=int, default=20000)
    ap.add_argument("--out", default="")
    ap.add_argument("--quiet", action="store_true")
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
    entries = load_entries(Path(args.dataset), args.start, args.num)
    out_path = Path(args.out) if args.out else None
    done = already_done(out_path) if out_path else set()

    rows = []
    fh = out_path.open("a", encoding="utf-8") if out_path else None
    if fh:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        for i, e in enumerate(entries, 1):
            if e["problem_idx"] in done:
                continue
            try:
                row = grade(e, args, baseline)
            except Exception as exc:  # noqa: BLE001  单题异常不中断整批
                row = {"problem_idx": e["problem_idx"], "task_name": e["task_name"],
                       "status": "harness_error",
                       "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
            rows.append(row)
            if fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            if not args.quiet:
                if row.get("status") == "ok":
                    print("[%d/%d] %5s %-30s tm=%8.4f rss=%6.1f ms=%8.3f cv=%.3f H1=%d H2=%d H3=%d"
                          % (i, len(entries), row["problem_idx"], row["task_name"][:30],
                             row["peak_tm_mb"], row["peak_rss_mb"], row["baseline_ms"],
                             row["cv"], row["pass_h1_space"], row["pass_h2_mu"],
                             row["pass_h3_time"]), flush=True)
                else:
                    print("[%d/%d] %5s %-30s %s"
                          % (i, len(entries), row["problem_idx"], row["task_name"][:30],
                             row.get("status")), flush=True)
    finally:
        if fh:
            fh.close()

    if out_path and out_path.exists():     # 续跑时汇总要包含历史行
        rows = []
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    print(summarize(rows, args, baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
