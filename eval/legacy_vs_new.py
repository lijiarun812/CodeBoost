"""旧口径 vs 新口径：量化「剖析不对称 + RSS 积分」造成的高估幅度。

两种用法：

1) 只读汇总（默认，秒级）
   py eval/legacy_vs_new.py
   对比中期旧跑（results/repro，4 模型 x 100 题，修复前口径）与期末新跑
   （results/matrix 等，4 模型 x 20 题，修复后口径）。两批题段不同，故只作
   分布对比；同一条记录内部的 TMU / 峰值两列才是配对样本。

2) 真配对重测（--remeasure N，零 API 成本，只用已存代码）
   py eval/legacy_vs_new.py --remeasure 8 --models deepseek-v4-pro,glm-4.6
   取旧数据里前 N 条 ok 记录，用「数据集 canonical 解 + 已存 final_code」按当前
   harness 重测，得到同一题同一份代码在两种口径下的降幅差 —— 这才是配对证据。

注意：本脚本不调模型 API，不修改任何已有结果文件。
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import harness

OLD_DIR = Path("results/repro")
NEW_GLOBS = ["results/matrix/*/repro_*.json"]
DATASET = Path("datasets/dataset.json")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ok(rec: dict) -> bool:
    return rec.get("status") == "ok" and rec.get("time_reduction_pct") is not None


def _mean(vals: list[float]) -> float | None:
    return round(st.mean(vals), 1) if vals else None


def _median(vals: list[float]) -> float | None:
    return round(st.median(vals), 1) if vals else None


def _key(name: str) -> str:
    """把 glm-4.6 / glm_4_6 / GLM-4-6 归一到同一个键。"""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def distribution_report() -> None:
    """旧跑 vs 新跑：分布对比（题段不同，非配对）。"""
    print("\n## 表 L1  旧口径 vs 新口径（分布对比，非配对；旧题段 3–246，新题段 250–314）\n")
    print("| 模型 | 旧:成功/总 | 旧:ET 降幅(均值/中位) | 旧:TMU 降幅(均值/中位) | 新:成功/总 | 新:ET 降幅 | 新:TMU 降幅 |")
    print("|---|---|---|---|---|---|---|")

    new_map = {}
    for pattern in NEW_GLOBS:
        for p in Path(".").glob(pattern):
            d = _load(p)
            if d.get("results"):
                new_map[_key(p.parent.name)] = d

    for p in sorted(OLD_DIR.glob("repro_*.json")):
        if "mock" in p.name:
            continue
        d = _load(p)
        rs = [r for r in d.get("results", []) if _ok(r)]
        et = [r["time_reduction_pct"] for r in rs]
        tm = [r["mem_reduction_pct"] for r in rs if r.get("mem_reduction_pct") is not None]
        model = p.stem.replace("repro_", "").replace("_", "-")
        nd = new_map.get(_key(model))
        if nd:
            nr = [r for r in nd["results"] if _ok(r)]
            net = [r["time_reduction_pct"] for r in nr]
            ntm = [r["mem_reduction_pct"] for r in nr if r.get("mem_reduction_pct") is not None]
            new_cells = "| %d/%d | %s%% | %s%% " % (
                len(nr), len(nd["results"]), _mean(net), _mean(ntm))
        else:
            new_cells = "| — | — | — "
        print("| %s | %d/%d | %s%% / %s%% | %s%% / %s%% %s|" % (
            model, len(rs), len(d.get("results", [])),
            _mean(et), _median(et), _mean(tm), _median(tm), new_cells))

    print("\n> 旧跑用的是修复前代码（基线与候选剖析不对称、内存为 RSS 积分），"
          "新跑为修复后口径。两批题段不重叠，故本表只说明量级差异，不作配对结论。")


def paired_in_record() -> None:
    """新数据内部：同一条记录的 TMU 降幅 vs 峰值降幅（真配对）。"""
    rows = []
    for pattern in NEW_GLOBS:
        for p in Path(".").glob(pattern):
            d = _load(p)
            for r in d.get("results", []):
                if _ok(r) and r.get("new_mem_reduction_pct") is not None:
                    rows.append((r["mem_reduction_pct"], r["new_mem_reduction_pct"]))
    if not rows:
        return
    diffs = [a - b for a, b in rows]
    big = sum(1 for d in diffs if abs(d) >= 5)
    print("\n## 表 L2  同一份代码：TMU 降幅 vs 峰值降幅（真配对，n=%d）\n" % len(rows))
    print("| 指标 | 数值 |")
    print("|---|---|")
    print("| TMU 降幅均值 | %s%% |" % _mean([a for a, _ in rows]))
    print("| 峰值降幅均值 | %s%% |" % _mean([b for _, b in rows]))
    print("| TMU 比峰值虚高（均值） | **%s 个百分点** |" % _mean(diffs))
    print("| 虚高幅度中位 | %s 个百分点 |" % _median(diffs))
    print("| 差值 ≥5 个百分点的记录数 | %d / %d |" % (big, len(rows)))
    print("\n> 中位为 0 说明多数题两者一致（峰值本就无变化），均值被少数「TMU 大幅下降而峰值不动」"
          "的记录拉高——这正是 TMU 制造「假空间收益」的形态，报告须给中位与分布，不可只报均值。")


def remeasure(count: int, models: list[str]) -> None:
    """对旧记录做真配对重测：同一题、同一份 final_code，换新口径重测。"""
    if not DATASET.exists():
        print("[skip] 缺少 %s，无法重测" % DATASET)
        return
    problems = {p["problem_idx"]: p for p in _load(DATASET)}
    print("\n## 表 L3  真配对重测（同题同代码，旧口径 vs 新口径，零 API 调用）\n")
    print("| 模型 | problem | 旧 ET 降幅 | 新 ET 降幅 | 高估 | 旧 TMU 降幅 | 新峰值降幅 |")
    print("|---|---|---|---|---|---|---|")

    tmp = Path(tempfile.mkdtemp(prefix="legacy_"))
    all_et, all_tm = [], []
    for model in models:
        path = next((p for p in OLD_DIR.glob("repro_*.json")
                     if _key(p.stem.replace("repro_", "")) == _key(model)), None)
        if path is None:
            continue
        d = _load(path)
        picked = [r for r in d["results"] if _ok(r) and r.get("final_code")][:count]
        for r in picked:
            pid = r["problem_idx"]
            prob = problems.get(pid)
            if not prob:
                continue
            test_block = prob["small_test_cases"]
            base_code = prob["canonical_solution"]
            final_code = r["final_code"]
            mb = harness.measure_code(base_code, test_block, kind="test_block",
                                      entry="solution", workdir=str(tmp))
            mf = harness.measure_code(final_code, test_block, kind="test_block",
                                      entry="solution", workdir=str(tmp))
            if mb.get("status") != "ok" or mf.get("status") != "ok":
                continue
            bt = mb["mean_trimmed_ms"]
            ft = mf["mean_trimmed_ms"]
            new_et = (bt - ft) / bt * 100
            bp = (mb.get("mem") or {}).get("peak_tracemalloc_mb")
            fp = (mf.get("mem") or {}).get("peak_tracemalloc_mb")
            new_tm = ((bp - fp) / bp * 100) if (bp and fp is not None and bp > 0) else None
            old_et = r["time_reduction_pct"]
            old_tm = r.get("mem_reduction_pct")
            all_et.append(old_et - new_et)
            if old_tm is not None and new_tm is not None:
                all_tm.append(old_tm - new_tm)
            print("| %s | %d | %s%% | %s%% | %+.1f | %s%% | %s |" % (
                model, pid, old_et, round(new_et, 1), old_et - new_et, old_tm,
                "n/a" if new_tm is None else "%.1f%%" % new_tm))
    print("\n**汇总**：旧口径 ET 降幅平均高估 **%s 个百分点**（n=%d）；"
          "TMU 相对峰值平均虚高 **%s 个百分点**（n=%d）。" % (
              _mean(all_et), len(all_et), _mean(all_tm), len(all_tm)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remeasure", type=int, default=0,
                    help="每模型重测前 N 条旧记录（0 表示只做汇总）")
    ap.add_argument("--models", default="deepseek-v4-pro,glm-4.6")
    args = ap.parse_args()

    print("# 旧口径 vs 新口径：偏差量化\n")
    distribution_report()
    paired_in_record()
    if args.remeasure > 0:
        remeasure(args.remeasure, [m.strip() for m in args.models.split(",") if m.strip()])


if __name__ == "__main__":
    main()
