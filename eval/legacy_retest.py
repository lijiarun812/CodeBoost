#!/usr/bin/env python3
"""旧口径 vs 新口径：用同一份 final_code，让两把尺子各量一次。

为什么这么重要：中期那 4 个模型 × 100 题的结果是**已交付成绩**，不能因为换了尺子
就作废；但也不能和新数字混表。正确做法是抽一批题，同一份代码分别用
旧口径（`final.exec_time / repeats`，psutil 积分那套）和新口径（harness E=12）测，
把“口径差多少”单独讲清楚。

不需要任何 API：代码就在结果 JSON 的 `final_code` 里。

用法：
    python eval/legacy_retest.py --per-model 6
    python eval/legacy_retest.py --per-model 3 --e-reps 6 --out docs/表格_旧口径vs新口径.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import harness  # noqa: E402


def load_repro(paths):
    out = []
    for p in sorted(paths):
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        if data.get("mode") == "mock":
            continue
        model = (data.get("mode") or Path(p).stem).replace("api:", "")
        recs = [r for r in (data.get("results") or [])
                if r.get("status") == "ok" and r.get("final_code") and r.get("repeats")]
        out.append((model, recs))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="旧口径 vs 新口径重测")
    ap.add_argument("--repro-dir", default=str(ROOT / "results" / "repro"))
    ap.add_argument("--dataset", default=str(ROOT / "datasets" / "dataset.json"))
    ap.add_argument("--per-model", type=int, default=6)
    ap.add_argument("--e-reps", type=int, default=harness.E_REPS)
    ap.add_argument("--out", default=str(ROOT / "docs" / "表格_旧口径vs新口径.md"))
    args = ap.parse_args(argv)

    sets = load_repro(Path(args.repro_dir).glob("repro_*.json"))
    if not sets:
        print("没有可用的 repro 结果", file=sys.stderr)
        return 2

    rows, missing, timeouts = [], 0, 0
    for model, recs in sets:
        picked = recs[:args.per_model]
        print("[%s] 重测 %d 题" % (model, len(picked)))
        for rec in picked:
            idx = str(rec["problem_idx"])
            tasks = harness.load_tasks("effibench", args.dataset, ids=[idx])
            if not tasks:
                missing += 1
                continue
            task = tasks[0]
            old_ms = rec["final"]["exec_time"] / float(rec["repeats"]) * 1000.0
            res = harness.measure_task(task, code=rec["final_code"],
                                       e_reps=args.e_reps)
            new_ms = res["time"]["median_ms"]
            status = res["status"]
            if status == "timeout":
                timeouts += 1
            rows.append({
                "model": model,
                "task": task["task_id"],
                "task_name": task.get("task_name", "")[:26],
                "repeats": rec["repeats"],
                "old_ms": round(old_ms, 4),
                "new_ms": None if new_ms is None else round(new_ms, 4),
                "ratio": (round(new_ms / old_ms, 3)
                          if (new_ms and old_ms) else None),
                "status": status,
            })
            print("   %-14s old=%.4f new=%s ratio=%s %s"
                  % (task["task_id"], old_ms,
                     "-" if new_ms is None else "%.4f" % new_ms,
                     "-" if new_ms is None else "%.2fx" % (new_ms / old_ms), status))

    ok = [r for r in rows if r["status"] == "ok" and r["ratio"]]
    ratios = [r["ratio"] for r in ok]
    lines = ["# 旧口径 vs 新口径（同一份 final_code）", "",
             "> 旧口径 = `final.exec_time / repeats`（中期 psutil 那套）；"
             "新口径 = `eval/harness.py` 的 E=%d 去极值。代码完全相同，只有尺子不同。" % args.e_reps,
             "> 结论只回答“**口径差多少**”，不重新解释方法效果。", "",
             "| 模型 | 题目 | 重复次数 | 旧口径 ms | 新口径 ms | 新/旧 | status |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s %s | %d | %.4f | %s | %s | %s |" % (
            r["model"], r["task"], r["task_name"], r["repeats"], r["old_ms"],
            "-" if r["new_ms"] is None else "%.4f" % r["new_ms"],
            "-" if r["ratio"] is None else "%.2fx" % r["ratio"], r["status"]))
    lines += ["", "## 汇总", "",
              "- 重测题数：**%d**（missing=%d，timeout=%d）" % (len(rows), missing, timeouts),
              "- 新/旧 比值中位数：**%s**"
              % ("-" if not ratios else "%.2fx" % statistics.median(ratios)),
              "- 新口径偏大：%d 题；偏小：%d 题"
              % (sum(1 for x in ratios if x > 1), sum(1 for x in ratios if x < 1)),
              "",
              "> 判读：比值系统性偏离 1 说明两把尺子的“绝对量级”不同（估计量 + 输入档不同）；"
              "真正要比方法效果时，必须在**同一把尺子**下比 baseline 与 candidate，不能跨口径比。"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out.parent / "legacy_retest.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已写入 %s" % out)
    print("汇总: %d 题，比值中位数 %s"
          % (len(rows), "-" if not ratios else "%.2fx" % statistics.median(ratios)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
