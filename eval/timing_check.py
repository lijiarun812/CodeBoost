#!/usr/bin/env python3
"""计时口径自证：同一份代码，四种测法的对照表。

用途：向组里/老师证明「harness 报的时间没有被 tracemalloc 污染」。
四种测法：
  ① 直接内联计时（最朴素：perf_counter 包住 N 次 check()）
  ② harness 的 time 档（关 tracemalloc 的子进程批量）
  ③ harness 最终报出的 median（产物字段）
  ④ 旧写法模拟：把 tracemalloc 开在计时循环里（应当显著更慢）

用法：
    python eval/timing_check.py --task 31 --n 300
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import harness  # noqa: E402


def direct_ms(code, test, entry, n=300):
    g = {"__name__": "__direct__"}
    exec(compile(code, "<sol>", "exec"), g)
    ns = dict(g)
    exec(compile(test, "<t>", "exec"), ns)
    check = ns["check"]
    cand = ns[entry]
    check(cand)  # 预热一次
    t0 = time.perf_counter()
    for _ in range(n):
        check(cand)
    return (time.perf_counter() - t0) / n * 1000.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="计时口径自证（四法对照）")
    ap.add_argument("--task", default="31")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--reps", type=int, default=300)
    ap.add_argument("--out", default=str(ROOT / "results" / "golden" / "timing_check.json"))
    args = ap.parse_args(argv)

    task = harness.load_tasks("humaneval", ROOT / "datasets" / "sample_humaneval_20.json",
                              ids=[args.task])[0]
    code, test, entry = task["code"], task["test"], task["entry_point"]

    d = direct_ms(code, test, entry, args.n)

    t_run = harness.run_child(
        harness._build_child(code, test, "humaneval", args.reps, harness.DEFAULT_MAX_MEM_MB,
                             entry, "time"), budget_s=120.0)
    time_ms = t_run.result["batch_s"] / args.reps * 1000.0 if t_run.result else None

    m_run = harness.run_child(
        harness._build_child(code, test, "humaneval", args.reps, harness.DEFAULT_MAX_MEM_MB,
                             entry, "mem"), budget_s=120.0)
    mem_ms = m_run.result["batch_s"] / args.reps * 1000.0 if m_run.result else None

    res = harness.measure_task(task, e_reps=6)
    reported = res["time"]["median_ms"]

    rows = [
        ("① 直接内联计时（perf_counter 包 %d 次）" % args.n, d, 1.0),
        ("② harness time 档（关 tracemalloc，%d 次/批）" % args.reps, time_ms,
         (time_ms / d) if (time_ms and d) else None),
        ("③ harness 报出的 median_ms", reported, (reported / d) if (reported and d) else None),
        ("④ 旧写法（tracemalloc 开在计时里）", mem_ms, (mem_ms / d) if (mem_ms and d) else None),
    ]
    print("%-44s %10s %8s" % ("测法", "单次 ms", "相对①"))
    for name, ms, ratio in rows:
        print("%-44s %10s %8s" % (name, "-" if ms is None else "%.4f" % ms,
                                  "-" if ratio is None else "%.2fx" % ratio))
    print()
    print("结论：②③ 与 ① 应接近（±20%%）；④ 明显更大 → 证明 tracemalloc 会污染计时，"
          "因此 harness 必须把内存单列一趟。")

    payload = {"task_id": task["task_id"], "n": args.n, "reps": args.reps,
               "direct_ms": round(d, 4),
               "time_mode_ms": None if time_ms is None else round(time_ms, 4),
               "mem_mode_ms": None if mem_ms is None else round(mem_ms, 4),
               "reported_median_ms": reported,
               "ratio_time_over_direct": None if not time_ms else round(time_ms / d, 3),
               "ratio_mem_over_direct": None if not mem_ms else round(mem_ms / d, 3)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已写入 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
