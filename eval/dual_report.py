"""双口径对照报告：同一批题、同一份 final_code，两种判据各算出多少降幅。

背景（为什么要这个脚本）：
- 论文口径（`mem_mbs` = 内存-时间积分）由旧实现近似得到，final 代码积分可能为 0，
  于是出现「内存下降 100%」这种不可信的数字；中期加权判据是另一套。
- 新口径（`proto-2026.11`，E=12 去极值 + 双峰值内存）与上面两者并行记录在同一份
  JSON 里（`new_baseline` / `new_final`），本脚本把三列并排算出来。

口径事实（必须先读，否则表 1 会被误读）：
- `mem_mbs = ∫RSS dt ≈ 峰值常驻内存 × 运行时长`，本质是**耗时代理**，不含独立的空间信息。
  证据见 `eval/check_mem_metric.py`：同一函数只加计算量、不加内存，峰值 22.2–22.5 MB
  几乎不变，`mem_mbs` 却从 0.0000 涨到 7.52、35.38，且 `mem_mbs / 耗时 ≈ 峰值`。
- 因此「论文口径内存降幅」实为「耗时降幅的投影」；代码够快时基线积分恰为 0，
  判据等价于「候选必须零分配」，结构上必然大量拒绝。
- 真正衡量空间的是 `peak_tracemalloc_mb` / `peak_rss_mb`，但本数据集题目偏小，
  tracemalloc 峰值常在亚 KB 级、前后完全相同（见表 4），需配合更大的输入档使用。

MU 口径（论文 Table 1 的另一列，我们此前漏测）：
- 论文同时报 **MU = Max memory Usage（峰值内存，MB）** 与 **TMU = Total（积分，MB·s）**。
  我们复现的 `mem_mbs` 只对应 TMU，而且只是它的**近似实现**——
  论文 GPT-3.5 的 `TMU ÷ ET = 437 MB`，比它自己的 MU（91 MB）还大 4.8 倍，
  说明两者统计范围不同，**绝对值不可与论文比较，只能比降幅**。
- 本脚本用 `peak_rss_mb` 对齐论文 MU（进程峰值），并额外给「扣解释器基线」版本：
  本机空载 RSS ≈ 21 MB，小题的 MU 会全部贴在这条基线上，不扣就看不出差别。
  但 RSS 采样噪声约 ±0.5 MB，扣完基线后小题只剩余噪声——**这一列只在 `peak_rss − baseline ≥ 5 MB` 时可信**。

输入：`repro_*.json`（effi_repro 产出，含 new_* 字段）
输出：Markdown 表格（stdout 或 --out 写文件）

用法：
    python eval/dual_report.py --inputs results/repro_dual/repro_*.json
    python eval/dual_report.py --inputs results/repro_dual/repro_x.json --out docs/表格_双口径.md
    python eval/dual_report.py --inputs ... --rss-baseline 20.8   # 手动指定空载基线
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FALSE_100 = 99.9  # 论文口径里 >= 这个数就视为「积分近似塌成 0」
MU_RESOLUTION_MB = 5.0  # peak_rss − 基线 低于这个数就认为 MU 列不可信（RSS 噪声 ~±0.5 MB）


def _reason(r) -> str:
    e = (r.get("error") or r.get("status") or "").strip()
    return e.split("\n")[0][:60]


def _pearson(xs, ys):
    """皮尔逊相关系数；样本不足或零方差时返回 None。"""
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sx = sum((a - mx) ** 2 for a in xs) ** 0.5
    sy = sum((b - my) ** 2 for b in ys) ** 0.5
    return cov / (sx * sy) if sx and sy else None


def measure_rss_baseline() -> float | None:
    """用同一把尺子量一次「空载」RSS：跑一个几乎不分配内存的程序，取 peak_rss。"""
    try:
        from eval import harness
    except Exception:
        return None
    code = "class Solution:\n    def f(self, x):\n        return x\n"
    test = "assert solution.f(1) == 1\n"
    try:
        m = harness.measure_code(code, test, kind="test_block", entry="solution",
                                 e_reps=2, target_batch_s=0.02, max_reps=50)
    except Exception:
        return None
    return (m.get("mem") or {}).get("peak_rss_mb")


def _mu_rows(ok: list, baseline: float | None) -> list[dict]:
    """逐题 MU（peak_rss）与扣基线版本；换不来数就跳过该题。"""
    rows = []
    for r in ok:
        nb = (r.get("new_baseline") or {}).get("peak_rss_mb")
        nf = (r.get("new_final") or {}).get("peak_rss_mb")
        if nb is None or nf is None or not nb:
            continue
        red = (nb - nf) / nb * 100.0
        delta_red = None
        db = df = None
        if baseline:
            db, df = max(0.0, nb - baseline), max(0.0, nf - baseline)
            if db > 0:
                delta_red = (db - df) / db * 100.0
        rows.append({"idx": r["problem_idx"], "nb": nb, "nf": nf, "red": red,
                     "db": db, "df": df, "delta_red": delta_red,
                     "resolved": (db is not None and db >= MU_RESOLUTION_MB)})
    return rows


def analyze(path: Path, rss_baseline: float | None = None) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    results = d.get("results", [])
    ok = [r for r in results if r.get("status") == "ok"]
    bad = [r for r in results if r.get("status") != "ok"]

    paper_mem = [r["mem_reduction_pct"] for r in ok if r.get("mem_reduction_pct") is not None]
    paper_time = [r["time_reduction_pct"] for r in ok if r.get("time_reduction_pct") is not None]
    new_mem = [r["new_mem_reduction_pct"] for r in ok if r.get("new_mem_reduction_pct") is not None]
    new_time = [r["new_time_reduction_pct"] for r in ok if r.get("new_time_reduction_pct") is not None]

    # TMU 与时间是不是同一个量：逐题配对求相关（同一批题、同一份 final_code）
    pairable = [(r["mem_reduction_pct"], r["time_reduction_pct"]) for r in ok
                if r.get("mem_reduction_pct") is not None and r.get("time_reduction_pct") is not None]
    tmu_vs_time_r = _pearson([a for a, _ in pairable], [b for _, b in pairable])

    mu_rows = _mu_rows(ok, rss_baseline)
    mu_red = [row["red"] for row in mu_rows]
    mu_delta = [row["delta_red"] for row in mu_rows if row["delta_red"] is not None]
    time_by_idx = {r["problem_idx"]: r.get("time_reduction_pct") for r in ok}
    mu_pairs = [(row["delta_red"], time_by_idx.get(row["idx"])) for row in mu_rows
                if row["delta_red"] is not None and time_by_idx.get(row["idx"]) is not None]
    mu_vs_time_r = _pearson([a for a, _ in mu_pairs], [b for _, b in mu_pairs])

    # 论文口径塌成 100% 但新口径给出有限值的题：假 100% 的直接证据
    fake = []
    for r in ok:
        pm = r.get("mem_reduction_pct")
        nm = r.get("new_mem_reduction_pct")
        if pm is not None and pm >= FALSE_100 and nm is not None:
            fake.append((r["problem_idx"], r["task_name"], pm, nm))

    # 内存列的分辨力：baseline 与 final 的 tracemalloc 峰值是否完全相同（分不出来）
    tm_pairs = [(r["new_baseline"]["peak_tracemalloc_mb"], r["new_final"]["peak_tracemalloc_mb"])
                for r in ok
                if r.get("new_baseline", {}).get("peak_tracemalloc_mb") is not None
                and r.get("new_final", {}).get("peak_tracemalloc_mb") is not None]
    ties = sum(1 for a, b in tm_pairs if a == b)
    tm_level = st.median([a for a, _ in tm_pairs]) if tm_pairs else None

    return {
        "file": path.name,
        "batch": path.parent.name,
        "model": d.get("mode", "?"),
        "dataset_problems": d.get("num_problems"),
        "epochs": d.get("epochs"),
        "accept_rule": d.get("accept_rule", "paper(默认)"),
        "n_ok": len(ok),
        "n_bad": len(bad),
        "bad_reasons": Counter(_reason(r) for r in bad),
        "paper_mem_avg": d.get("avg_mem_reduction_pct"),
        "paper_mem_median": st.median(paper_mem) if paper_mem else None,
        "paper_time_avg": d.get("avg_time_reduction_pct"),
        "paper_time_median": st.median(paper_time) if paper_time else None,
        "new_mem_mean": st.mean(new_mem) if new_mem else None,
        "new_mem_median": st.median(new_mem) if new_mem else None,
        "new_time_mean": st.mean(new_time) if new_time else None,
        "new_time_median": st.median(new_time) if new_time else None,
        "n_fake_100": len(fake),
        "fake_examples": fake[:5],
        "n_mem_tie": ties,
        "n_mem_pairs": len(tm_pairs),
        "tm_level_mb": tm_level,
        "n_new_mem": len(new_mem),
        "paper_gt0_mem": sum(1 for m in paper_mem if m > 0),
        "cv_list": [r["new_baseline"]["cv"] for r in ok
                    if r.get("new_baseline", {}).get("cv") is not None],
        # MU（论文 Table 1 的另一列）
        "rss_baseline": rss_baseline,
        "mu_red_mean": st.mean(mu_red) if mu_red else None,
        "mu_red_median": st.median(mu_red) if mu_red else None,
        "mu_delta_mean": st.mean(mu_delta) if mu_delta else None,
        "mu_delta_median": st.median(mu_delta) if mu_delta else None,
        "mu_n": len(mu_rows),
        "mu_resolved": sum(1 for row in mu_rows if row["resolved"]),
        "mu_peak_median": st.median([row["nb"] for row in mu_rows]) if mu_rows else None,
        "tmu_vs_time_r": tmu_vs_time_r,
        "mu_vs_time_r": mu_vs_time_r,
    }


def label(r) -> str:
    """模型名 + 批次目录，避免「同一模型跑两批」在表里出现两行同名。"""
    model = r["model"].replace("api:", "")
    batch = r["batch"]
    return model if model == batch else "%s（%s）" % (model, batch)


def fmt(v, nd=1):
    return "n/a" if v is None else ("%+.1f%%" % v)


def md(reports: list) -> str:
    out = []
    w = out.append
    w("### 表 1  TMU（积分）与时间：同一批题、同一份 final_code\n")
    w("| 模型 | 题数(成功/总数) | TMU（本实现近似）ᵃ(均值/中位) | 中期加权 积分ᵇ(均值) | tracemalloc 峰值ᶜ(均值/中位) | 时间 论文口径 | 时间 新口径 |")
    w("|---|---|---|---|---|---|---|")
    for r in reports:
        w("| %s | %d/%d | %s / %s | %s | %s / %s | %s | %s |" % (
            label(r), r["n_ok"], r["dataset_problems"],
            fmt(r["paper_mem_avg"]), fmt(r["paper_mem_median"]),
            "—",  # 中期加权判据的复现结果在另一份 JSON（accept-rule=weighted）
            fmt(r["new_mem_mean"]), fmt(r["new_mem_median"]),
            fmt(r["paper_time_avg"]), fmt(r["new_time_mean"])))
    w("")
    w("> ᵃ **TMU（本实现近似）**：`mem_mbs = ∫RSS dt ≈ 峰值 × 耗时`，是**内存与时间的综合指标**；"
      "但在本实现里它与耗时高度共线（见表 5：r 在 0.50–0.97 之间、随批次变化），**因此不能当作独立的「空间收益」**；"
      "决定性证据是受控实验而不是相关系数——"
      "证据见 `eval/check_mem_metric.py`（同函数只加计算量、不加内存，峰值不变而 mem_mbs 涨三个数量级）。"
      "论文的 TMU 绝对值与我们不同口径（论文 GPT-3.5 的 `TMU÷ET = 437 MB` 比其 MU 91 MB 还大 4.8 倍），"
      "**绝对值不可比，只能比降幅**。")
    w("> ᵇ 中期加权判据同样建立在积分上，故同一问题。")
    w("> ᶜ `peak_tracemalloc_mb` 只覆盖 **Python 堆**（不含 C 扩展/numpy），且本数据集题目偏小，"
      "分辨力见表 4；进程级空间口径见 **表 1b 的 MU**。")
    w("")
    w("### 表 1b  MU（峰值内存，对齐论文 Table 1 的 MU 列）\n")
    w("| 模型 | 题数 | MU = peak_rss 均值/中位降幅 | MU − 空载基线 均值降幅 | 基线 RSS | 可分辨题数 |")
    w("|---|---|---|---|---|---|")
    for r in reports:
        base = ("—" if r["rss_baseline"] is None else "%.1f MB" % r["rss_baseline"])
        # 一题都不可分辨时，均值是噪声放大器，宁可不给数
        delta_cell = (fmt(r["mu_delta_mean"]) if r["mu_resolved"]
                      else "n/a（无 ≥%.0f MB 的题）" % MU_RESOLUTION_MB)
        w("| %s | %d | %s / %s | %s | %s | %d/%d |" % (
            label(r), r["mu_n"],
            fmt(r["mu_red_mean"]), fmt(r["mu_red_median"]),
            delta_cell, base,
            r["mu_resolved"], r["mu_n"]))
    w("")
    w("> 「MU − 空载基线」= `max(0, peak_rss − baseline)`；只有该值 ≥ %.0f MB 的题才算**可分辨**"
      "（RSS 采样噪声约 ±0.5 MB，小题扣完基线只剩噪声）。" % MU_RESOLUTION_MB)
    w("")
    w("### 表 2  「假 100%」清查\n")
    w("| 模型 | TMU 口径记 100% 的题 | 新口径能给出有限值的题 | 说明 |")
    w("|---|---|---|---|")
    for r in reports:
        w("| %s | %d | %d | %s |" % (
            label(r), r["n_fake_100"], r["n_ok"],
            "积分近似在 final 上塌成 0；但这是积分实现的问题，不代表真实空间占用降低 100%"))
    w("")
    for r in reports:
        if r["fake_examples"]:
            w("**%s 假 100%% 样例**（论文口径 / 新口径）" % label(r))
            for pid, name, pm, nm in r["fake_examples"]:
                w("- `idx=%s` %s：论文 %+.1f%% → 新口径 %+.1f%%" % (pid, name, pm, nm))
            w("")
    w("### 表 3  失败原因分布（新口径未测到 = 失败，不计入降幅）\n")
    w("| 模型 | 失败/总数 | 主要原因 |")
    w("|---|---|---|")
    for r in reports:
        reasons = "；".join("%s ×%d" % (k or "(空)", v) for k, v in r["bad_reasons"].most_common(3))
        w("| %s | %d/%d | %s |" % (
            label(r), r["n_bad"], r["dataset_problems"], reasons or "—"))
    w("")
    w("### 表 4  新口径内存列的分辨力（能分开 baseline 与 final 吗）\n")
    w("| 模型 | baseline 峰值中位 | peak 完全相同的题 | 说明 |")
    w("|---|---|---|---|")
    for r in reports:
        note = ("峰值只有亚 KB 级、题目太小，内存列**没有分辨力**"
                if r["n_mem_pairs"] and r["n_mem_tie"] / r["n_mem_pairs"] >= 0.8
                else "内存列可用")
        w("| %s | %s | %d/%d | %s |" % (
            label(r),
            "n/a" if r["tm_level_mb"] is None else "%.4f MB" % r["tm_level_mb"],
            r["n_mem_tie"], r["n_mem_pairs"], note))
    w("")
    w("### 表 5  「TMU 是不是时间的投影」——逐题配对相关\n")
    w("| 模型 | r(TMU降幅, 时间降幅) | r(MU−基线降幅, 时间降幅) | 判读 |")
    w("|---|---|---|---|")
    for r in reports:
        rt, rm = r["tmu_vs_time_r"], r["mu_vs_time_r"]
        verdict = ("TMU 与时间高度共线 → 不能当空间指标"
                   if rt is not None and rt >= 0.8 else
                   ("本批不共线（但仍需按受控实验判断：" 
                    "`mem_mbs/耗时 ≈ peak` 与 check_mem_metric 才是决定性证据）"
                    if rt is not None else "样本不足"))
        w("| %s | %s | %s | %s |" % (
            label(r),
            "n/a" if rt is None else "%.3f" % rt,
            "n/a" if rm is None else "%.3f" % rm,
            verdict))
    w("")
    cv = [c for r in reports for c in r["cv_list"]]
    if cv:
        w("新口径基线 CV：中位 %.1f%%，范围 %.1f%%–%.1f%%（%d 题），"
          "噪声地板 1+2CV ≈ %.2f×。" % (
              st.median(cv) * 100, min(cv) * 100, max(cv) * 100, len(cv), 1 + 2 * st.median(cv)))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True, help="repro_*.json（支持通配）")
    ap.add_argument("--out", default="", help="写入 md 文件（缺省打印）")
    ap.add_argument("--json-out", default="", help="同时导出汇总 JSON")
    ap.add_argument("--rss-baseline", type=float, default=None,
                    help="空载 RSS 基线（MB）；缺省自动用 harness 量一次，写 results/rss_baseline.json 缓存")
    a = ap.parse_args()

    baseline = a.rss_baseline
    if baseline is None:
        cache = REPO_ROOT / "results" / "rss_baseline.json"
        if cache.exists():
            try:
                baseline = json.loads(cache.read_text(encoding="utf-8")).get("peak_rss_mb")
            except Exception:
                baseline = None
        if baseline is None:
            baseline = measure_rss_baseline()
            if baseline is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(
                    {"peak_rss_mb": baseline,
                     "how": "harness.measure_code(class Solution: f(x)->x, kind=test_block, e_reps=2)"},
                    ensure_ascii=False, indent=2), encoding="utf-8")
        if baseline is not None:
            print("空载 RSS 基线 = %.2f MB" % baseline)

    files = []
    for pat in a.inputs:
        files.extend(sorted(glob.glob(pat)) or [pat])
    reports = [analyze(Path(f), baseline) for f in files if Path(f).exists()]
    if not reports:
        raise SystemExit("没有可读的输入文件")

    text = md(reports)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print("wrote %s" % a.out)
    else:
        print(text)
    if a.json_out:
        slim = [{k: v for k, v in r.items() if k not in ("bad_reasons", "fake_examples", "cv_list")}
                for r in reports]
        for s, r in zip(slim, reports):
            s["bad_reasons"] = dict(r["bad_reasons"])
        Path(a.json_out).write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote %s" % a.json_out)


if __name__ == "__main__":
    main()
