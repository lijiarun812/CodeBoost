"""双口径对照报告：同一批题、同一份 final_code，两种判据各算出多少降幅。

背景（为什么要这个脚本）：
- 论文口径（`mem_mbs` = 内存-时间积分）由旧实现近似得到，final 代码积分可能为 0，
  于是出现「内存下降 100%」这种不可信的数字；中期加权判据是另一套。
- 新口径（`proto-2026.11`，E=12 去极值 + 双峰值内存）与上面两者并行记录在同一份
  JSON 里（`new_baseline` / `new_final`），本脚本把三列并排算出来。

输入：`repro_*.json`（effi_repro 产出，含 new_* 字段）
输出：Markdown 表格（stdout 或 --out 写文件）

用法：
    python eval/dual_report.py --inputs results/repro_dual/repro_*.json
    python eval/dual_report.py --inputs results/repro_dual/repro_x.json --out docs/表格_双口径.md
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics as st
from collections import Counter
from pathlib import Path

FALSE_100 = 99.9  # 论文口径里 >= 这个数就视为「积分近似塌成 0」


def _reason(r) -> str:
    e = (r.get("error") or r.get("status") or "").strip()
    return e.split("\n")[0][:60]


def analyze(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    results = d.get("results", [])
    ok = [r for r in results if r.get("status") == "ok"]
    bad = [r for r in results if r.get("status") != "ok"]

    paper_mem = [r["mem_reduction_pct"] for r in ok if r.get("mem_reduction_pct") is not None]
    paper_time = [r["time_reduction_pct"] for r in ok if r.get("time_reduction_pct") is not None]
    new_mem = [r["new_mem_reduction_pct"] for r in ok if r.get("new_mem_reduction_pct") is not None]
    new_time = [r["new_time_reduction_pct"] for r in ok if r.get("new_time_reduction_pct") is not None]

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
    }


def fmt(v, nd=1):
    return "n/a" if v is None else ("%+.1f%%" % v)


def md(reports: list) -> str:
    out = []
    w = out.append
    w("### 表 1  同一批题、同一份 final_code 下的三列降幅\n")
    w("| 模型 | 题数(成功/总数) | 论文口径 内存(均值/中位) | 中期加权 内存(均值) | 新口径 内存(均值/中位) | 论文口径 时间 | 新口径 时间 |")
    w("|---|---|---|---|---|---|---|")
    for r in reports:
        w("| %s | %d/%d | %s / %s | %s | %s / %s | %s | %s |" % (
            r["model"].replace("api:", ""), r["n_ok"], r["dataset_problems"],
            fmt(r["paper_mem_avg"]), fmt(r["paper_mem_median"]),
            "—",  # 中期加权判据的复现结果在另一份 JSON（accept-rule=weighted）
            fmt(r["new_mem_mean"]), fmt(r["new_mem_median"]),
            fmt(r["paper_time_avg"]), fmt(r["new_time_mean"])))
    w("")
    w("### 表 2  「假 100%」清查\n")
    w("| 模型 | 论文口径记 100% 的题 | 新口径能给出有限值的题 | 说明 |")
    w("|---|---|---|---|")
    for r in reports:
        w("| %s | %d | %d | %s |" % (
            r["model"].replace("api:", ""), r["n_fake_100"], r["n_ok"],
            "旧口径积分近似在 final 上塌成 0；新口径用 tracemalloc 峰值，不会塌 0"))
    w("")
    for r in reports:
        if r["fake_examples"]:
            w("**%s 假 100%% 样例**（论文口径 / 新口径）" % r["model"].replace("api:", ""))
            for pid, name, pm, nm in r["fake_examples"]:
                w("- `idx=%s` %s：论文 %+.1f%% → 新口径 %+.1f%%" % (pid, name, pm, nm))
            w("")
    w("### 表 3  失败原因分布（新口径未测到 = 失败，不计入降幅）\n")
    w("| 模型 | 失败/总数 | 主要原因 |")
    w("|---|---|---|")
    for r in reports:
        reasons = "；".join("%s ×%d" % (k or "(空)", v) for k, v in r["bad_reasons"].most_common(3))
        w("| %s | %d/%d | %s |" % (
            r["model"].replace("api:", ""), r["n_bad"], r["dataset_problems"], reasons or "—"))
    w("")
    w("### 表 4  新口径内存列的分辨力（能分开 baseline 与 final 吗）\n")
    w("| 模型 | baseline 峰值中位 | peak 完全相同的题 | 说明 |")
    w("|---|---|---|---|")
    for r in reports:
        note = ("峰值只有亚 KB 级、题目太小，内存列**没有分辨力**"
                if r["n_mem_pairs"] and r["n_mem_tie"] / r["n_mem_pairs"] >= 0.8
                else "内存列可用")
        w("| %s | %s | %d/%d | %s |" % (
            r["model"].replace("api:", ""),
            "n/a" if r["tm_level_mb"] is None else "%.4f MB" % r["tm_level_mb"],
            r["n_mem_tie"], r["n_mem_pairs"], note))
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
    a = ap.parse_args()

    files = []
    for pat in a.inputs:
        files.extend(sorted(glob.glob(pat)) or [pat])
    reports = [analyze(Path(f)) for f in files if Path(f).exists()]
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
