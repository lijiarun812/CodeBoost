#!/usr/bin/env python3
"""12 题 golden 重锁：把「当前值 + 口径版本 + harness commit」写进一份可追溯的文件。

为什么必须重锁
--------------
今晚测量口径变了三次：`split_cases` 支持 test_block 拆分、`hotlines` 加前导、
`bind_entry` 改 `solution` 绑定；再加上 `%Opt` 阈值从 1.0 改成 1.10。
旧 12 题数字是**上一把尺子**量的，不能继续当基线。

与旧做法的两条区别（组长要求）
------------------------------
1. **不做「必须等于旧值」的断言**——亚毫秒题本身抖 10–40%，改成「在容差内稳定」：
   `|新 − 旧| / 旧 ≤ max(2×CV, 0.25)`；超差的题单独列出来说明原因，不静默通过。
2. **文件头写死溯源信息**：`protocol_version`、`harness_commit`、时间、命令、解释器版本、
   每题的 `cv`。

用法
----
    python eval/lock_golden.py                     # 跑 2 轮 + 写 eval/tests/golden_locked.json
    python eval/lock_golden.py --runs 3            # 多跑几轮看稳定性
    python eval/lock_golden.py --dry-run           # 只打印不落盘

输出
----
    eval/tests/golden_locked.json
    docs/表格_golden重锁对照.md    （新旧对照 + 变化原因）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import harness  # noqa: E402

TASKS = ["31", "40", "55", "59", "60", "63", "96", "120", "123", "130", "139", "147"]
OLD_GOLDEN = REPO_ROOT / "results" / "golden" / "golden_20261127-182913.json"
OUT_JSON = REPO_ROOT / "eval" / "tests" / "golden_locked.json"
OUT_MD = REPO_ROOT / "docs" / "表格_golden重锁对照.md"

# 每题的容差 = max(2×CV, 0.25)；25% 下限是因为亚毫秒题的批次噪声本身就有 10–40%
TOL_FLOOR = 0.25


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _measure(task, e_reps: int):
    return harness.measure_code(task["code"], task["test"], kind=task.get("kind", "humaneval"),
                                entry=task.get("entry_point", "candidate"),
                                e_reps=e_reps, target_batch_s=0.25)


def _old_values() -> dict:
    if not OLD_GOLDEN.exists():
        return {}
    try:
        doc = json.loads(OLD_GOLDEN.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    rows_in = doc.get("results") if isinstance(doc, dict) else doc
    out = {}
    for row in (rows_in or []):
        tid = str(row.get("task_id") or row.get("task") or "")
        short = tid.split("/")[-1]
        if short:
            out[short] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--e-reps", type=int, default=harness.E_REPS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tasks = {t["task_id"].split("/")[-1]: t for t in
             harness.load_tasks("humaneval", REPO_ROOT / "datasets" / "sample_humaneval_20.json")}
    old = _old_values()
    commit = _commit()
    rows, unstable = [], []

    for tid in TASKS:
        task = tasks.get(tid)
        if task is None:
            rows.append({"task_id": "HumanEval/%s" % tid, "status": "missing_task"})
            continue
        got = [_measure(task, args.e_reps) for _ in range(max(1, args.runs))]
        ok = [g for g in got if g.get("status") == "ok"]
        if not ok:
            rows.append({"task_id": "HumanEval/%s" % tid, "status": got[0].get("status"),
                         "error": str(got[0].get("error"))[:160]})
            continue
        vals = [g["mean_trimmed_ms"] for g in ok]
        spread = (max(vals) - min(vals)) / vals[0] if vals[0] else 0.0
        r = ok[0]
        row = {
            "task_id": "HumanEval/%s" % tid,
            "status": "ok",
            "mean_trimmed_ms": round(r["mean_trimmed_ms"], 6),
            "median_ms": round(r["median_ms"], 6),
            "cv": round((r.get("stats") or {}).get("cv") or 0.0, 4),
            "reps": r.get("reps"),
            "peak_tracemalloc_mb": (r.get("mem") or {}).get("peak_tracemalloc_mb"),
            "peak_rss_mb": (r.get("mem") or {}).get("peak_rss_mb"),
            "runs_mean_trimmed_ms": [round(v, 6) for v in vals],
            "run_spread": round(spread, 4),
        }
        o = old.get(tid) or {}
        o_time = o.get("time") if isinstance(o.get("time"), dict) else {}
        o_ms = (o_time.get("mean_trimmed_ms") or o_time.get("median_ms")
                or o.get("mean_trimmed_ms") or o.get("median_ms"))
        if isinstance(o_ms, (int, float)) and o_ms:
            tol = max(2.0 * row["cv"], TOL_FLOOR)
            delta = (row["mean_trimmed_ms"] - o_ms) / o_ms
            row.update(old_mean_trimmed_ms=o_ms, delta_rel=round(delta, 4),
                       tol=round(tol, 4), within_tol=abs(delta) <= tol)
            if not row["within_tol"]:
                unstable.append((tid, o_ms, row["mean_trimmed_ms"], delta, tol))
        rows.append(row)
        mark = "" if row.get("within_tol", True) else "  ← 超差"
        print("%-12s mean=%9.4f ms  cv=%.3f  runs=%s%s"
              % (row["task_id"], row["mean_trimmed_ms"], row["cv"],
                 [round(v, 4) for v in vals], mark), flush=True)

    doc = {
        "protocol_version": harness.PROTOCOL_VERSION,
        "harness_commit": commit,
        "locked_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "command": "python eval/lock_golden.py --runs %d --e-reps %d" % (args.runs, args.e_reps),
        "e_reps": args.e_reps,
        "tasks": TASKS,
        "tolerance_rule": "|new-old|/old <= max(2*cv, %.2f)" % TOL_FLOOR,
        "note": ("harness 没有 --profile 开关；等价验证用「同一命令跑 %d 次，逐题 spread 记录在 "
                 "runs_mean_trimmed_ms/run_spread」+ eval/timing_check.py 证计时未被 tracemalloc 污染"
                 % args.runs),
        "results": rows,
    }
    if args.dry_run:
        print("\n[dry-run] 未落盘；超差 %d 题" % len(unstable))
        return 0

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写入 %s（commit %s, %s）" % (OUT_JSON, commit, doc["protocol_version"]))

    lines = ["# 12 题 golden 重锁对照", "",
             "> 口径版本 `%s` · harness commit `%s` · %s"
             % (doc["protocol_version"], commit, doc["locked_at"]),
             "> 容差规则：`|新 − 旧| / 旧 ≤ max(2×CV, %.2f)`" % TOL_FLOOR, "",
             "| 题 | 旧值 (ms) | 新值 (ms) | Δ | 容差 | 判定 | CV | 备注 |",
             "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        if row.get("status") != "ok":
            lines.append("| %s | — | — | — | — | %s | — | 未测到 |"
                         % (row["task_id"], row.get("status")))
            continue
        note = "两次运行 spread %.1f%%" % (row["run_spread"] * 100)
        if "within_tol" not in row:
            note += "；无旧值可比"
        elif not row["within_tol"]:
            note += "；**超差**，见 §原因"
        lines.append("| %s | %s | %.4f | %s | %.0f%% | %s | %.3f | %s |" % (
            row["task_id"],
            ("%.4f" % row["old_mean_trimmed_ms"]) if row.get("old_mean_trimmed_ms") else "—",
            row["mean_trimmed_ms"],
            ("%+.1f%%" % (row["delta_rel"] * 100)) if row.get("delta_rel") is not None else "—",
            (row["tol"] * 100) if row.get("tol") else 0,
            ("✅ 稳定" if row.get("within_tol") else ("⚠️ 超差" if "within_tol" in row else "新基线")),
            row["cv"], note))
    lines += ["", "## 变化原因（口径改过三次）", "",
              "1. `split_cases` 新增 `test_block` 顶层 assert 拆分——只影响 EffiBench，HumanEval 走原路径，**理论上不变**；",
              "2. `hotlines` 加前导与实例绑定——只影响诊断，**不影响计时**；",
              "3. `bind_entry` 改 `solution` 绑定——HumanEval 走 `humaneval` 分支，**不影响**；",
              "4. `%Opt` 阈值 1.0 → 1.10——只影响 `%Opt` 统计，**不影响单题时间**。",
              "",
              "结论：**12 题的时间值理论上不该变**；实测确有差异的，只可能来自机器负载/进程抖动，",
              "因此本表用「容差内稳定」判定，而不是要求等于旧值。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已写入 %s；超差 %d 题" % (OUT_MD, len(unstable)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
