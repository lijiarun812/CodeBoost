#!/usr/bin/env python3
"""对组长已提交的 EffiLearner 复现结果做「零成本重分析」。

不需要 API Key、不需要重跑：`results/repro/repro_*.json` 里每题都存了
baseline / final 的 exec_time(s)、mem_mbs(MB*s)、peak_mb，足以重算三套口径：

1. 旧口径（组长现在用的）：直接比 mem_mbs 的下降百分比；
2. 归一化口径：mem_mb_avg = mem_mbs / exec_time（等效常驻内存），
   消除“跑得快 → 时间积分小”的耦合，也是“假 100%”问题的正解；
3. 时间口径：exec_time 的下降百分比。

同时统计三类“可疑样本”：
- 零积分题：final 的 mem_mbs ≈ 0（README 里说的“假 100%”）；
- 亚毫秒题：baseline exec_time < 1ms（测量噪声大于优化收益）；
- 被内存判据误杀的题：时间变快 >5%，但 mem_mbs 没下降 → 旧判据会丢弃它。

用法：
    python eval/reanalyze_repro.py                      # 终端打印 + 写 docs 表格
    python eval/reanalyze_repro.py --out docs/表格_复现口径对比.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZERO_INTEGRAL_EPS = 1e-9
SUB_MS = 1.0  # ms


def _num(x):
    return x if isinstance(x, (int, float)) else None


def analyze_record(rec):
    b, f = rec.get("baseline") or {}, rec.get("final") or {}
    bt, ft = _num(b.get("exec_time")), _num(f.get("exec_time"))
    bm, fm = _num(b.get("mem_mbs")), _num(f.get("mem_mbs"))
    if rec.get("status") != "ok" or not bt or not ft:
        return None

    old_mem = (bm - fm) / bm * 100.0 if (bm and fm is not None) else None
    b_avg = bm / bt if bm else None
    f_avg = fm / ft if fm is not None else None
    new_mem = (b_avg - f_avg) / b_avg * 100.0 if b_avg else None
    time_red = (bt - ft) / bt * 100.0

    return {
        "task": rec.get("task_name") or str(rec.get("problem_idx")),
        "base_ms": round(bt * 1000.0, 4),
        "time_red": round(time_red, 1),
        "old_mem": None if old_mem is None else round(old_mem, 1),
        "new_mem": None if new_mem is None else round(new_mem, 1),
        "zero_integral": fm is not None and fm <= ZERO_INTEGRAL_EPS,
        "sub_ms": bt * 1000.0 < SUB_MS,
        # 时间明显变快、但内存口径几乎不变 → 纯内存判据“看不见”的收益
        "time_gain_mem_blind": bool(time_red > 5.0 and (old_mem is None or old_mem < 5.0)),
        # 供 ratio-of-means（先求和再算比例）聚合使用：mem_mbs 的基线量级差好几个数量级，
        # 逐题算比例再平均会被小基线题放大，先求和更稳。
        "b_s": bt, "f_s": ft,
        "b_mem": bm or 0.0, "f_mem": fm or 0.0,
        "b_avg": b_avg, "f_avg": f_avg,
    }


def _rom(pairs):
    """ratio of means：1 - Σfinal / Σbaseline，返回百分数。"""
    sb = sum(b for b, _ in pairs)
    sf = sum(f for _, f in pairs)
    if not sb:
        return None
    return round((sb - sf) / sb * 100.0, 1)


def summarize(tasks):
    def vals(key, pool=None):
        pool = tasks if pool is None else pool
        return [t[key] for t in pool if t[key] is not None]

    measurable = [t for t in tasks if not t["zero_integral"]]
    out = {
        "n": len(tasks),
        "n_meas": len(measurable),
        "n_zero_integral": sum(1 for t in tasks if t["zero_integral"]),
        "n_sub_ms": sum(1 for t in tasks if t["sub_ms"]),
        "n_faster_but_rejected": sum(1 for t in tasks if t["time_gain_mem_blind"]),
    }
    for key, name in (("old_mem", "old"), ("new_mem", "new"), ("time_red", "time")):
        v = vals(key)
        out[name + "_mean"] = round(statistics.mean(v), 1) if v else None
        out[name + "_median"] = round(statistics.median(v), 1) if v else None

    # 主口径：ratio-of-means（先求和再算比例），对大基线题更稳
    out["rom_old_all"] = _rom([(t["b_mem"], t["f_mem"]) for t in tasks])
    out["rom_old_meas"] = _rom([(t["b_mem"], t["f_mem"]) for t in measurable])
    out["rom_new_all"] = _rom([(t["b_avg"], t["f_avg"]) for t in tasks
                               if t["b_avg"] and t["f_avg"] is not None])
    out["rom_new_meas"] = _rom([(t["b_avg"], t["f_avg"]) for t in measurable
                                if t["b_avg"] and t["f_avg"] is not None])
    out["rom_time_all"] = _rom([(t["b_s"], t["f_s"]) for t in tasks])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="EffiLearner 复现结果的口径重分析")
    ap.add_argument("--repro-dir", default=str(ROOT / "results" / "repro"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "表格_复现口径对比.md"))
    args = ap.parse_args(argv)

    files = sorted(Path(args.repro_dir).glob("repro_*.json"))
    if not files:
        print("没找到 %s/repro_*.json" % args.repro_dir)
        return 2

    per_model, all_tasks = {}, []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mode") == "mock":
            continue
        tasks = [t for t in (analyze_record(r) for r in data.get("results") or []) if t]
        if not tasks:
            continue
        model = (data.get("mode") or path.stem).replace("api:", "")
        per_model[model] = {"summary": summarize(tasks), "tasks": tasks,
                            "num_problems": data.get("num_problems"),
                            "num_ok": data.get("num_ok")}
        all_tasks.extend(tasks)

    if not per_model:
        print("没有可分析的真实结果（只有 mock）。")
        return 2

    lines = ["# EffiLearner 复现：旧口径 vs 归一化口径", "",
             "> 由 `eval/reanalyze_repro.py` 自动生成；数据源 `results/repro/repro_*.json`（组长提交，只读）。",
             "> 只做数值重算，不重新执行代码：结果 JSON 里存了 baseline/final 的 exec_time 与 mem_mbs。", ""]

    lines += ["## 一、整体结论", ""]
    overall = summarize(all_tasks)
    lines += [
        "| 项 | 值 |", "|---|---|",
        "| 纳入统计的题数 | %d |" % overall["n"],
        "| 时间下降（先求和再算比例） | %s%% |" % overall["rom_time_all"],
        "| 内存下降 ①旧口径 mem_mbs（全部题） | %s%% |" % overall["rom_old_all"],
        "| 内存下降 ②旧口径 + 剔除零积分题 | %s%% |" % overall["rom_old_meas"],
        "| **内存下降 ③归一化 + 剔除零积分题（可测口径）** | **%s%%**（n=%d） |" % (
            overall["rom_new_meas"], overall["n_meas"]),
        "| （参考）逐题平均口径：旧 %s%% / 归一化 %s%% | — |" % (
            overall["old_mean"], overall["new_mean"]),
        "| **零积分题（≈假 100%%）** | **%d 道（%.1f%%）** |" % (
            overall["n_zero_integral"], 100.0 * overall["n_zero_integral"] / overall["n"]),
        "| 亚毫秒题（基线 <1ms） | %d 道（%.1f%%） |" % (
            overall["n_sub_ms"], 100.0 * overall["n_sub_ms"] / overall["n"]),
        "| 时间↓>5%% 但内存口径≈0（内存判据看不见的收益） | %d 道 |" % overall["n_faster_but_rejected"],
        "",
        "**为什么从 ①到③差这么多**：`mem_mbs` 是 RSS-时间积分，近似等于「平均常驻内存 × 执行时间」，"
        "所以代码变快本身就会让它下降——①里的降幅大部分是**时间收益的机械副作用**，不是真的更省内存。"
        "除以 `exec_time` 得到等效常驻内存后，剩下的才是真正的内存收益；再剔除积分恒为 0 的题（采样点不足），"
        "得到的就是敢在答辩里报的 ③。三者满足近似乘法关系："
        "1-(1-③)(1-时间下降) ≈ ①，可直接用数字自检。",
        "",
        "> 局限：结果 JSON 只存了**被采纳**的 final，所以「时间变快但被内存判据直接丢弃」的候选无法从已提交数据里观察到。"
        "要看到它，需要 `effilearner/effi_repro.py` 额外保存每轮候选代码（已列入 `docs/优化计划.md`）。",
        "",
    ]

    lines += ["## 二、分模型", "",
              "| 模型 | 成功题 | ①旧口径(全部) | ②旧口径(剔除零积分) | ③归一化可测 | 时间↓ | 零积分题 | 内存盲区 |",
              "|---|---|---|---|---|---|---|---|"]
    for model, item in per_model.items():
        s = item["summary"]
        lines.append("| %s | %d/%s | %s%% | %s%% | %s%% | %s%% | %d | %d |" % (
            model, s["n"], item.get("num_problems") or "?",
            s["rom_old_all"], s["rom_old_meas"], s["rom_new_meas"], s["rom_time_all"],
            s["n_zero_integral"], s["n_faster_but_rejected"]))
    lines.append("")

    lines += ["## 三、逐题明细（按时间收益排序）", "",
              "| 任务 | 基线(ms) | 时间↓ | 旧口径内存↓ | 归一化内存↓ | 标记 |",
              "|---|---|---|---|---|---|"]
    for t in sorted(all_tasks, key=lambda x: -(x["time_red"] or 0))[:40]:
        flags = []
        if t["zero_integral"]:
            flags.append("零积分")
        if t["sub_ms"]:
            flags.append("亚毫秒")
        if t["time_gain_mem_blind"]:
            flags.append("内存盲区")
        lines.append("| %s | %.4f | %s%% | %s | %s | %s |" % (
            t["task"][:28], t["base_ms"], t["time_red"],
            "—" if t["old_mem"] is None else "%s%%" % t["old_mem"],
            "—" if t["new_mem"] is None else "%s%%" % t["new_mem"],
            "、".join(flags)))
    lines.append("")
    lines.append("> 明细最多列 40 行，完整数据见上面「分模型」统计或原始 JSON。")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines[:40]))
    print("\n已写入 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

