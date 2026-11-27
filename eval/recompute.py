#!/usr/bin/env python3
"""用**当前 harness** 重算已存实验结果的新口径（不调模型、不花 API）。

为什么需要它
------------
矩阵是在口径变动过程中跑出来的：有的记录 `new_baseline/new_final` 是 `n/a`（当时测不出），
有的是旧估计量测的。这些题不该重新调模型——`final_code` 已经落盘，本地重测就行。

三条硬约束
----------
1. **不调 API**：只读已存的 `final_code`；
2. **不覆盖原文件**：输出 `<原名>_patched.json`，原件一字不动；
3. **能算的算、不能算的标清楚**：每条记录带 `recompute` 块，状态只有三种
   `recomputed` / `retryable` / `excluded`。

关键实现事实（踩过才知道）
--------------------------
- effi_repro 用的是 `problem["small_test_cases"]`（不是 `test_case`），
  而 `small_test_cases` 里**用到了 `build_tree` / `build_tree_from_list` / `TreeNode`**，
  这些 helper 在 effi 自己的 `build_program()` 里注入，但 `new_metrics()` 直接调
  harness 时**没有注入** → 250 / 285 当年就是这样变成 `n/a` 的。
  本脚本把同一份 helper 前导补上，于是这两题可以真正补算（不是「数据集缺陷」）。
- `new_baseline` **无法重算**：模型生成的初始代码从未落盘（只存了 `final_code`）。
  这一项保持原值并标 `new_baseline_note`，不要用 canonical 冒充。

分类规则
--------
| 情况 | 标记 |
|---|---|
| 补前导后 `final_code` 可测 | `recomputed` |
| `final_code` 不可测，但 canonical（同样补前导）**也**不可测 | `excluded: dataset_defect` |
| `final_code` 不可测，canonical 可测 | `retryable: candidate_incorrect`（模型写错，需重跑） |
| 记录里没有 `final_code` | `retryable: final_code_missing` |

用法
----
    python eval/recompute.py --inputs results/matrix/*/repro_*.json
    python eval/recompute.py --inputs results/matrix/glm-4.6/repro_glm_4_6.json --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "effilearner") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "effilearner"))

from eval import harness  # noqa: E402

DEFAULT_DATASET = REPO_ROOT / "datasets" / "dataset.json"


def _helper_prelude() -> str:
    """与 effi_repro.build_program() 注入的 helper 保持同一份（Tree/List/build_*）。"""
    try:
        import effi_repro as E  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    parts = [getattr(E, n, "") for n in ("IMPORT_PKG", "TREE_NODE", "LIST_NODE", "BUILD_HELPERS")]
    return "\n".join(p for p in parts if p)


def _measure(code: str, test: str, *, e_reps: int, target_batch_s: float, max_reps: int):
    return harness.measure_code(code, test, kind="test_block", entry="solution",
                                e_reps=e_reps, target_batch_s=target_batch_s,
                                max_reps=max_reps, stale_s=30)


def _is_na(v) -> bool:
    return v is None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True, help="repro_*.json（支持通配）")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--e-reps", type=int, default=harness.E_REPS)
    ap.add_argument("--target-batch-s", type=float, default=0.25)
    ap.add_argument("--max-reps", type=int, default=20000)
    ap.add_argument("--dry-run", action="store_true", help="只分类不写文件")
    args = ap.parse_args()

    prelude = _helper_prelude()
    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    by_idx = {str(e.get("problem_idx")): e for e in data}

    files: list[Path] = []
    for pat in args.inputs:
        files.extend(sorted(glob.glob(pat)) or ([pat] if Path(pat).exists() else []))
    if not files:
        print("没有匹配的输入文件")
        return 2

    for path in files:
        path = Path(path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        rows = doc.get("results") or []
        na_before = sum(1 for r in rows if _is_na(r.get("new_mem_reduction_pct")))
        print("\n=== %s：%d 条记录，重算前 new_* 为 n/a 的 %d 条 ===" % (path, len(rows), na_before))
        canon_cache: dict[str, bool] = {}
        recount = {"recomputed": 0, "retryable": 0, "excluded": 0}
        for r in rows:
            idx = str(r.get("problem_idx"))
            entry = by_idx.get(idx)
            rc = {"status": "retryable", "reason": "", "new_baseline_note": "baseline_code_not_stored"}
            if entry is None:
                rc.update(status="excluded", reason="problem_not_in_dataset")
            elif not (r.get("final_code") or "").strip():
                # 没存下 final_code：先判这题到底能不能测——canonical 也过不了就是数据集缺陷
                if idx not in canon_cache:
                    cm = _measure(prelude + "\n" + (entry.get("canonical_solution") or ""),
                                  entry.get("small_test_cases") or "",
                                  e_reps=2, target_batch_s=0.05, max_reps=200)
                    canon_cache[idx] = cm.get("status") == "ok"
                if canon_cache[idx]:
                    rc.update(status="retryable", reason="final_code_missing")
                    recount["retryable"] += 1
                else:
                    rc.update(status="excluded", reason="dataset_defect",
                              error_at_recompute="canonical 也过不了 small_test_cases")
                    recount["excluded"] += 1
            else:
                test = entry.get("small_test_cases") or ""
                m = _measure(prelude + "\n" + r["final_code"], test,
                             e_reps=args.e_reps, target_batch_s=args.target_batch_s,
                             max_reps=args.max_reps)
                if m.get("status") == "ok":
                    mem = m.get("mem") or {}
                    st = m.get("stats") or {}
                    r["new_final"] = {
                        "status": "ok",
                        "mean_trimmed_ms": m.get("mean_trimmed_ms"),
                        "median_ms": m.get("median_ms"),
                        "cv": st.get("cv"),
                        "peak_tracemalloc_mb": mem.get("peak_tracemalloc_mb"),
                        "peak_rss_mb": mem.get("peak_rss_mb"),
                        "reps": m.get("reps"),
                    }
                    nb_t = (r.get("new_baseline") or {}).get("mean_trimmed_ms")
                    nb_m = (r.get("new_baseline") or {}).get("peak_tracemalloc_mb")
                    nf_t = r["new_final"]["mean_trimmed_ms"]
                    nf_m = r["new_final"]["peak_tracemalloc_mb"]
                    if nb_t and nf_t:
                        r["new_time_reduction_pct"] = round((nb_t - nf_t) / nb_t * 100, 1)
                    if nb_m and nf_m:
                        r["new_mem_reduction_pct"] = round((nb_m - nf_m) / nb_m * 100, 1)
                    rc.update(status="recomputed",
                              reason="ok", error_at_recompute=None)
                    recount["recomputed"] += 1
                else:
                    if idx not in canon_cache:
                        cm = _measure(prelude + "\n" + (entry.get("canonical_solution") or ""),
                                      entry.get("small_test_cases") or "",
                                      e_reps=2, target_batch_s=0.05, max_reps=200)
                        canon_cache[idx] = cm.get("status") == "ok"
                    if canon_cache[idx]:
                        rc.update(status="retryable", reason="candidate_incorrect",
                                  error_at_recompute=str(m.get("error"))[:160])
                        recount["retryable"] += 1
                    else:
                        rc.update(status="excluded", reason="dataset_defect",
                                  error_at_recompute=str(m.get("error"))[:160])
                        recount["excluded"] += 1
            r["recompute"] = rc
        na_after = sum(1 for r in rows if _is_na(r.get("new_mem_reduction_pct")))
        doc["recompute_meta"] = {
            "source": str(path),
            "protocol_version": harness.PROTOCOL_VERSION,
            "helper_prelude": "effi_repro.IMPORT_PKG+TREE_NODE+LIST_NODE+BUILD_HELPERS",
            "new_baseline": "not_recomputable: baseline_code_not_stored",
            "counts": recount,
            "n_na_before": na_before,
            "n_na_after": na_after,
        }
        for r in rows:
            rc = r.get("recompute") or {}
            if rc.get("status") != "recomputed":
                print("  [%s] idx=%-4s %-28s %s %s" % (
                    rc.get("status"), r.get("problem_idx"), (r.get("task_name") or "")[:28],
                    rc.get("reason"), (rc.get("error_at_recompute") or "")[:60]))
        print("  重算后 n/a：%d → %d ｜ recomputed=%d retryable=%d excluded=%d"
              % (na_before, na_after, recount["recomputed"], recount["retryable"], recount["excluded"]))
        if not args.dry_run:
            out = path.with_name(path.stem + "_patched.json")
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            print("  已写入 %s（原文件未动）" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
