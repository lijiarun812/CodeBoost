#!/usr/bin/env python3
"""把「论文数值 + 我们的复现结果」拼成可直接贴上 PPT 的对照表。

输出 `docs/表格_论文vs复现.md`，包含三张表：
1. EffiLearner：论文 vs 我们的 4 个模型（含口径修正前后）；
2. PerfCodeGen：论文 vs 我们的结果（没跑过就标注"待跑"）；
3. 与论文设置的差异（评分表"复现"里必须写的一列）。

数据源：
- 论文：`eval/paper_numbers.py`（每个数字都标了 PDF 页码）
- 我们的 EffiLearner：`results/repro/repro_*.json`（组长提交，只读）
- 我们的 PerfCodeGen：`results/perfcodegen/<model>/summary.json`

用法：
    python eval/make_table.py
    python eval/make_table.py --out docs/表格_论文vs复现.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.paper_numbers import DIFFS, EFFIBENCH, EFFILEARNER, PERFCODEGEN  # noqa: E402
from eval.reanalyze_repro import analyze_record, summarize  # noqa: E402


def load_effi(repro_dir: Path):
    out = {}
    for path in sorted(repro_dir.glob("repro_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mode") == "mock":
            continue
        tasks = [t for t in (analyze_record(r) for r in (data.get("results") or [])) if t]
        if not tasks:
            continue
        model = (data.get("mode") or path.stem).replace("api:", "")
        out[model] = {
            "summary": summarize(tasks),
            "num_problems": data.get("num_problems"),
            "num_ok": data.get("num_ok"),
        }
    return out


def load_pcg(pcg_dir: Path):
    rows = []
    for path in sorted(pcg_dir.glob("*/summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("model") or "") == "mock":
            continue
        rows.append(data)
    return rows


def build_section_effi(effi):
    lines = ["## 一、EffiLearner：论文 vs 复现", ""]
    lines += ["**论文结果**（%s）" % EFFILEARNER["source"], "",
              "| 模型 | 指标 | 初始 | 优化后 | 论文报告下降 |",
              "|---|---|---|---|---|"]
    for model, metrics in EFFILEARNER["models"].items():
        for name, (before, after, cut) in metrics.items():
            lines.append("| %s | %s（%s） | %s | %s | %s |" % (
                model, name, EFFILEARNER["metric_units"][name], before, after, cut))
    lines += ["", "**我们的复现**（EffiBench 前 100 题 × 3 轮，API 调用）", "",
              "| 模型 | 成功题 | 时间下降 | 内存下降（旧口径 mem_mbs） | 内存下降（归一化 + 剔除零积分题） | 零积分题 |",
              "|---|---|---|---|---|---|"]
    for model, item in effi.items():
        s = item["summary"]
        lines.append("| %s | %d/%s | %s%% | %s%% | %s%% | %d |" % (
            model, s["n"], item.get("num_problems") or "?",
            s["rom_time_all"], s["rom_old_all"], s.get("rom_new_meas"), s["n_zero_integral"]))
    lines += ["",
              "> 口径说明：`mem_mbs` 是 RSS-时间积分（≈平均常驻内存 × 执行时间），代码变快本身就会让它下降；"
              "「归一化 + 剔除零积分题」= 先用 `mem_mbs/exec_time` 去掉时间因素，再剔掉积分恒为 0 的题（采样点不足），"
              "剩下的才是真正的内存收益。三者近似满足 `1-(1-可测口径)(1-时间下降) ≈ 旧口径`，可用数字自检。",
              "> 结论方向与论文一致（带开销反馈的迭代能降低时间/内存），但**绝对幅度不可直接比较**："
              "模型、题目规模、测量方式都不同。", ""]
    return lines


def build_section_pcg(rows):
    lines = ["## 二、PerfCodeGen：论文 vs 复现", "",
             "**论文结果**（%s）" % PERFCODEGEN["source"], "",
             "| 项 | 论文 | 我们 |", "|---|---|---|",
             "| GPT-4 上被优化到优于 ground truth 的题目比例（HumanEval） | ~%d%% | 见下表 |"
             % PERFCODEGEN["gpt4_opt_humaneval_pct"],
             "| 同上（MBPP） | ~%d%% | 本次未跑 MBPP |" % PERFCODEGEN["gpt4_opt_mbpp_pct"],
             ""]
    if not rows:
        lines += ["> 还没有 `results/perfcodegen/<model>/summary.json`。先跑：",
                  "> `python perfcodegen/pcg_repro.py --provider deepseek --model deepseek-chat --num 20 --samples 5`",
                  ""]
        return lines
    lines += ["**我们的复现**", "",
              "| 模型 | 题目数 | %Correct | Speedup(avg) | %Opt(avg) | Best@5 | 最终版 Speedup（vs base） | 最终版 %Opt（vs base） | 剔除极端 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for d in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            d.get("model"), d.get("n_tasks"), d.get("pct_correct"),
            d.get("speedup_avg"), d.get("pct_opt_avg"), d.get("speedup@5"),
            d.get("speedup_vs_base_avg"), d.get("pct_opt_vs_base"),
            d.get("n_dropped_extreme", 0)))
    strategy = sorted({d.get("time_strategy") for d in rows if d.get("time_strategy")})
    lines += ["", "> 反馈策略：%s；采样数：%s；测量相对 MAD 中位数：%s" % (
        "、".join(strategy) or "—",
        "、".join(str(d.get("samples")) for d in rows),
        "、".join(str(d.get("median_rel_mad")) for d in rows)), ""]
    return lines


def build_section_diff():
    lines = ["## 三、与论文设置的差异（这一节答辩必问）", "",
             "| 维度 | 论文 | 我们 |", "|---|---|---|"]
    for dim, paper, ours in DIFFS:
        lines.append("| %s | %s | %s |" % (dim, paper, ours))
    lines.append("")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成论文 vs 复现对照表")
    ap.add_argument("--repro-dir", default=str(ROOT / "results" / "repro"))
    ap.add_argument("--pcg-dir", default=str(ROOT / "results" / "perfcodegen"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "表格_论文vs复现.md"))
    args = ap.parse_args(argv)

    effi = load_effi(Path(args.repro_dir))
    pcg = load_pcg(Path(args.pcg_dir))

    lines = ["# 论文 vs 复现：可直接上 PPT 的对照表", "",
             "> 由 `eval/make_table.py` 生成；论文数字全部来自 `eval/paper_numbers.py`（附 PDF 页码），"
             "我们的数字来自 `results/`。", "",
             "**评测基准**：%s（%d 题 / 评测 %d 个模型）。%s" % (
                 EFFIBENCH["paper"], EFFIBENCH["n_problems"],
                 EFFIBENCH["n_models_evaluated"], EFFIBENCH["headline"]), "",
             "> 出处：%s" % EFFIBENCH["source"], ""]
    lines += build_section_effi(effi)
    lines += build_section_pcg(pcg)
    lines += build_section_diff()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print("\n已写入 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

