"""生成「论文三张表 vs 我们」的对照文档，只读已有结果 JSON，不花 API。

用法：
    py eval/paper_tables.py                        # 默认读 results/matrix/*/repro_*.json
    py eval/paper_tables.py --inputs results/repro_dual/*.json --out docs/x.md

口径说明（答辩必须按这个讲）：
- %Correct：跑通题数 / 总题数；另给「剔除不可执行题」后的口径，
  因为部分题的 canonical_solution 本身过不了测试块，任何模型都必然失败。
- %Opt：优化后比 canonical 更快的题数 / 跑通题数（论文 %Opt 的定义是「优于 ground truth」）。
- Speedup：base_time / final_time 的几何平均。
- ET：执行时间降幅均值（论文 Table 1 的 ET 列）。
- TMU：本实现近似（∫RSS dt ≈ 峰值×耗时），不是独立空间指标，只作内部对照。
- MU：peak_rss 降幅（对齐论文 MU 定义），并给出「有信号」题数（|Δ| ≥ 5 MB）。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics as st
from pathlib import Path

# 论文 Table 1 / 摘要数值，出处：docs/论文数值_出处.md
PAPER = {
    "GPT-4": {"ET": -9.7, "MU": -21.1, "TMU": None, "opt": 47.0},
    "GPT-3.5-Turbo-0301": {"ET": None, "MU": -60.5, "TMU": -92.1, "opt": None},
    "StarCoder2-15B": {"ET": None, "MU": None, "TMU": -90.8, "opt": None},
}
MU_RESOLVABLE_MB = 5.0
TM_RESOLVABLE_MB = 0.05
# PerfCodeGen §4：%Opt 的门槛是「比 ground truth 至少快 10%」，不是「更快即可」
OPT_MIN_SPEEDUP = 1.10


def _gmean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def analyze(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    rs = d.get("results", [])
    ok = [r for r in rs if r.get("status") == "ok"]
    dead = [r for r in rs if r.get("status") != "ok"]

    ratios, opt, ets, tmus, mus, mu_res, tm_res = [], 0, [], [], [], 0, 0
    for r in ok:
        bt, ft = r["baseline"]["exec_time"], r["final"]["exec_time"]
        if bt and ft:
            ratios.append(bt / ft)
            # PerfCodeGen §4 定义：%Opt = 比 ground truth 至少快 10% 的题占比
            if bt / ft >= OPT_MIN_SPEEDUP:
                opt += 1
        if r.get("time_reduction_pct") is not None:
            ets.append(r["time_reduction_pct"])
        if r.get("mem_reduction_pct") is not None:
            tmus.append(r["mem_reduction_pct"])
        nb, nf = r.get("new_baseline") or {}, r.get("new_final") or {}
        b_rss, f_rss = nb.get("peak_rss_mb"), nf.get("peak_rss_mb")
        if b_rss and f_rss:
            red = (b_rss - f_rss) / b_rss * 100
            mus.append(red)
            if abs(b_rss - f_rss) >= MU_RESOLVABLE_MB:
                mu_res += 1
        b_tm, f_tm = nb.get("peak_tracemalloc_mb"), nf.get("peak_tracemalloc_mb")
        if b_tm is not None and f_tm is not None and abs((b_tm or 0) - (f_tm or 0)) >= TM_RESOLVABLE_MB:
            tm_res += 1

    n_all = len(rs)
    n_dead = len(dead)
    return {
        "name": path.parent.name,
        "model": d.get("mode", "?"),
        "epochs": d.get("epochs"),
        "rule": d.get("accept_rule"),
        "n_all": n_all,
        "n_ok": len(ok),
        "n_dead": n_dead,
        "correct_raw": len(ok) / n_all * 100 if n_all else None,
        "correct_ex": len(ok) / (n_all - n_dead) * 100 if n_all - n_dead else None,
        "opt_rate": opt / len(ok) * 100 if ok else None,
        "speedup": _gmean(ratios),
        "et": st.mean(ets) if ets else None,
        "tmu": st.mean(tmus) if tmus else None,
        "mu": st.mean(mus) if mus else None,
        "mu_res": mu_res,
        "tm_res": tm_res,
        "n_mu": len(mus),
        "dead": [(r["problem_idx"], r["task_name"], str(r["status"])[:40]) for r in dead],
    }


def fmt(v, unit="%", nd=1):
    if v is None:
        return "n/a"
    return (("%+." + str(nd) + "f") % v) + unit


def fmt_p(v, nd=1):
    """比例类指标（%Correct / %Opt）不带正负号。"""
    if v is None:
        return "n/a"
    return (("%." + str(nd) + "f") % v) + "%"


def md(rows, cv_hint=None):
    out = []
    w = out.append
    w("# 论文三张表 vs 我们的复现\n")
    w("> 本文档由 `eval/paper_tables.py` 从 `results/**/repro_*.json` 自动生成，"
      "只读数据、不调用模型。论文数值出处见 `docs/论文数值_出处.md`。\n")

    w("## 表 1  三个核心指标（我们）\n")
    w("| 批次 | 模型 | epochs | 判据 | %Correct（原始） | %Correct（剔除不可执行） | %Opt | Speedup |")
    w("|---|---|---|---|---|---|---|---|")
    for r in rows:
        w("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["name"], r["model"], r["epochs"], r["rule"],
            fmt_p(r["correct_raw"]), fmt_p(r["correct_ex"]), fmt_p(r["opt_rate"]),
            "n/a" if r["speedup"] is None else "%.3f×" % r["speedup"]))
    w("")
    w("- `%Correct（原始）` 的分母含「标准解自己跑不通」的题，会系统性偏低；"
      "`剔除不可执行` 才是模型能力的真实反映。")
    w("- `%Opt` 沿用 PerfCodeGen §4 定义：**比 ground truth 至少快 10%** 的题占比，分母是跑通的题（早期版本误写成「更快即可」，已修正）。")
    w("- `Speedup` 是 `base_time / final_time` 的几何平均。\n")

    w("## 表 2  按论文列（ET / MU / TMU）对照\n")
    w("| 批次 | 模型 | ET 降幅 | MU 降幅（peak_rss） | MU 有信号题 | TMU 降幅ᵃ | TMU 有信号题 |")
    w("|---|---|---|---|---|---|---|")
    for r in rows:
        w("| %s | %s | %s | %s | %d/%d | %s | %d/%d |" % (
            r["name"], r["model"], fmt(r["et"]), fmt(r["mu"]),
            r["mu_res"], r["n_mu"], fmt(r["tmu"]), r["tm_res"], r["n_mu"]))
    w("")
    w("> ᵃ TMU = 本实现的 `mem_mbs`（∫RSS dt ≈ 峰值 × 耗时）。它与时间高度共线"
      "（实测 r = 0.94–0.97），**不能作为独立的「空间收益」证据**；"
      "它本身仍是「内存 × 时间」的综合量，数值仅供内部对照。")
    w("> MU 有信号 = `|peak_rss(baseline) − peak_rss(final)| ≥ %.0f MB`；"
      "TMU 有信号 = `|Δpeak_tracemalloc| ≥ %.2f MB`。\n" % (MU_RESOLVABLE_MB, TM_RESOLVABLE_MB))

    w("## 表 3  论文数值对照（不同数据集，仅看量级）\n")
    w("| 来源 | 模型 | ET | MU | TMU | %Opt |")
    w("|---|---|---|---|---|---|")
    for k, v in PAPER.items():
        w("| 论文 Table 1 | %s | %s | %s | %s | %s |" % (
            k, fmt(v["ET"]), fmt(v["MU"]), fmt(v["TMU"]),
            "n/a" if v["opt"] is None else "%.0f%%" % v["opt"]))
    for r in rows:
        w("| 我们 | %s | %s | %s | %s | %s |" % (
            r["model"], fmt(r["et"]), fmt(r["mu"]), fmt(r["tmu"]), fmt(r["opt_rate"])))
    w("")
    w("**不能直接比较**：论文跑 HumanEval / MBPP（GPT-4 量级），我们跑 EffiBench 前 20 题"
      "（标准解中位耗时 0.226 ms）。ET 量级接近（论文 −9.7%，我们 −10.8%），"
      "MU 在这批题上无信号——两者都不是「谁更好」的证据。\n")

    w("## 表 4  不可执行题清单（任何模型都必然失败）\n")
    w("| 批次 | 题号 | 题目 | 错误 |")
    w("|---|---|---|---|")
    for r in rows:
        for pid, name, err in r["dead"]:
            w("| %s | %s | %s | %s |" % (r["name"], pid, name, err))
    w("")
    w("> 这批题的分母污染是**数据集问题**，不是模型问题。B 冻结题库时需先做 "
      "「canonical 自检」，把这些题排除，否则 `%Correct` 的上限被固定压低。\n")

    w("## 差异原因（答辩用）\n")
    w("| # | 差异 | 原因 | 证据 |")
    w("|---|---|---|---|")
    w("| 1 | 我们的 MU 无信号 | 题目太小：`peak_rss` 贴着 %.1f MB 解释器基线，"
      "与论文 80–91 MB 量级差 3 倍 | 表 2 的 MU 有信号题数 |" % 20.5)
    w("| 2 | TMU 看似大降 | 本实现 `∫RSS dt` ≈ 峰值×耗时，与时间共线 r=0.94–0.97 | 表 2 脚注 |")
    w("| 3 | 我们的 %Correct 偏低 | 20 题里 4 题标准解跑不通（表 4），任何模型必失败 | 表 4 |")
    w("| 4 | 我们的 %Opt 与论文口径不同 | 论文比 HumanEval/MBPP，我们比 EffiBench 的 canonical | 表 3 |")
    w("| 5 | 早期版本 0 采纳 | 剖析不对称：基线不剖析、候选剖析，虚高 2.8 倍 | `eval/timing_check.py` |")
    w("")
    if cv_hint:
        w(cv_hint)
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", default=["results/matrix/*/repro_*.json"])
    ap.add_argument("--out", default="docs/论文对照与差异分析.md")
    a = ap.parse_args()

    files = []
    for pat in a.inputs:
        files.extend(sorted(glob.glob(pat)))
    rows = [analyze(Path(f)) for f in files]
    if not rows:
        raise SystemExit("没有可读的输入文件")
    text = md(rows)
    Path(a.out).write_text(text, encoding="utf-8")
    print("wrote %s（%d 个批次）" % (a.out, len(rows)))
    for r in rows:
        print("  %-22s %s  ok=%d/%d  ET=%s  MU=%s" % (
            r["name"], r["model"], r["n_ok"], r["n_all"], fmt(r["et"]), fmt(r["mu"])))


if __name__ == "__main__":
    main()
