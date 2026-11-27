"""
分析 results/results.json：
- 每题 4 组方法的正确率、最慢档耗时、speedup
- 汇总：变快/变慢/破坏正确性题数
- 失败案例 A-E 分类（对齐大作业 docx）
- 输出 Markdown 报告 + HTML 报告
"""
import json
import os
from statistics import median

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(HERE, "results", "results_v2.json")
REPORT_MD = os.path.join(HERE, "results", "report_v2.md")
REPORT_HTML = os.path.join(HERE, "results", "report_v2.html")


def slowest(times):
    cand = [t for t in times if "median_ms" in t]
    if not cand:
        return None
    return max(cand, key=lambda x: x["median_ms"])


def analyze():
    data = json.load(open(RESULTS_FILE, encoding="utf-8"))
    rows = []
    for tid, r in data.items():
        base_correct = r.get("effective_base_correct", r.get("base_correct", False))
        base_t = slowest(r.get("base_times", [])) if base_correct else None
        row = {
            "task_id": tid,
            "entry": r.get("entry_point"),
            "base_correct": base_correct,
            "base_err": r.get("base_check_err"),
            "base_ms": base_t["median_ms"] if base_t else None,
        }
        for m in ["vanilla", "perfcodegen", "ours"]:
            row[f"{m}_correct"] = r.get(f"{m}_correct", False)
            t = next((x for x in r.get(f"{m}_times", [])
                      if base_t and x.get("desc") == base_t.get("desc") and "median_ms" in x), None)
            row[f"{m}_ms"] = t["median_ms"] if t else None
            if base_t and t and t["median_ms"] > 0 and base_t["median_ms"] > 0:
                row[f"{m}_speedup"] = round(base_t["median_ms"] / t["median_ms"], 2)
            else:
                row[f"{m}_speedup"] = None
        rows.append(row)
    return rows


def classify_failures(rows):
    """对齐 docx：A 已近最优 / B 找错瓶颈 / C 快但错 / D 微改 / E 计时噪声"""
    classes = {"A_already_optimal": [], "B_wrong_bottleneck": [],
               "C_fast_but_wrong": [], "D_minor_change": [], "E_noise": []}
    for r in rows:
        base_ms = r["base_ms"]
        if not r["base_correct"]:
            continue  # base 就错的题，不算优化失败
        for m in ["vanilla", "perfcodegen", "ours"]:
            opt_ms = r[f"{m}_ms"]
            sp = r[f"{m}_speedup"]
            opt_ok = r[f"{m}_correct"]
            if not opt_ok:
                classes["C_fast_but_wrong"].append(
                    (r["task_id"], m, "优化后未通过 HumanEval check()；未测量性能，不能判定更快"))
                continue
            if sp is None:
                continue
            # E：基线本身 < 1ms，speedup 看起来 10% 但其实是测量噪声
            if base_ms is not None and base_ms < 1.0:
                classes["E_noise"].append(
                    (r["task_id"], m, f"基线仅 {base_ms:.3f}ms，speedup {sp}x 在噪声范围内"))
                continue
            if sp >= 1.3:
                continue  # 真正优化成功
            if sp < 0.95:
                classes["B_wrong_bottleneck"].append(
                    (r["task_id"], m, f"反而变慢 {sp}x，LLM 改坏了"))
            elif sp <= 1.05:
                classes["D_minor_change"].append(
                    (r["task_id"], m, f"speedup {sp}x，算法层面无实质改动"))
            else:
                classes["A_already_optimal"].append(
                    (r["task_id"], m, f"基线已接近最优，speedup {sp}x"))
    return classes


def render_md(rows, classes):
    n_base_ok = sum(1 for r in rows if r["base_correct"])
    lines = []
    lines.append("# 执行反馈粒度对 LLM 代码性能优化影响 — 实验报告\n")
    lines.append("## 0. 关键发现\n")
    lines.append(f"- 模型：本地 Qwen2.5-Coder-7B（Ollama），数据集：HumanEval 子集 {len(rows)} 题。")
    lines.append(f"- Base 阶段 LLM 写出正确代码的题数：{n_base_ok}/{len(rows)}。")
    # 统计三组方法的真正优化成功（speedup>=1.3）数
    for m in ["vanilla", "perfcodegen", "ours"]:
        success = sum(1 for r in rows
                      if r[f"{m}_speedup"] and r[f"{m}_speedup"] >= 1.3 and r[f"{m}_correct"])
        broke = sum(1 for r in rows if r["base_correct"] and not r[f"{m}_correct"])
        lines.append(f"- {m}: 真正优化成功 {success} 题，破坏正确性 {broke} 题。")
    lines.append("- 本报告仅汇总预实验；短任务的速度差异需重复验证，不能直接认定为有效提升。\n")

    lines.append("## 1. 逐题结果（最慢档输入耗时 ms）\n")
    lines.append("| Task | entry | Base ms | Vanilla ms (sp) | PerfCodeGen ms (sp) | Ours ms (sp) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        def cell(m):
            ms = r[f"{m}_ms"]
            sp = r[f"{m}_speedup"]
            ok = r[f"{m}_correct"]
            if not r["base_correct"]:
                return "—"
            if not ok:
                return "❌wrong"
            if ms is None:
                return "-"
            return f"{ms:.2f} ({sp}x)"
        base_ms_str = f"{r['base_ms']:.3f}" if r['base_ms'] is not None else "BASE_WRONG"
        lines.append(f"| {r['task_id']} | {r['entry']} | {base_ms_str} | "
                     f"{cell('vanilla')} | {cell('perfcodegen')} | {cell('ours')} |")

    lines.append("\n## 2. 汇总\n")
    for m in ["vanilla", "perfcodegen", "ours"]:
        sps = [r[f"{m}_speedup"] for r in rows
               if r[f"{m}_speedup"] and r["base_correct"]]
        faster = sum(1 for s in sps if s >= 1.3)
        slower = sum(1 for s in sps if s < 0.95)
        broke = sum(1 for r in rows if r["base_correct"] and not r[f"{m}_correct"])
        avg_sp = round(sum(sps)/len(sps), 2) if sps else "-"
        lines.append(f"- **{m}**: avg speedup={avg_sp}x, 显著变快(≥1.3x)={faster}, "
                     f"变慢(<0.95x)={slower}, 破坏正确性={broke}")

    lines.append("\n## 3. 失败案例分类（对齐大作业 docx A-E）\n")
    label = {"A_already_optimal": "A. 原始代码已接近最优",
             "B_wrong_bottleneck": "B. LLM 找错瓶颈",
             "C_fast_but_wrong": "C. 优化后破坏正确性（速度未知）",
             "D_minor_change": "D. 算法没变/微改",
             "E_noise": "E. 计时噪声"}
    for k in ["A_already_optimal", "B_wrong_bottleneck", "C_fast_but_wrong",
              "D_minor_change", "E_noise"]:
        items = classes[k]
        lines.append(f"\n### {label[k]}（{len(items)}）")
        for tid, m, why in items:
            lines.append(f"- {tid} [{m}]: {why}")

    md = "\n".join(lines)
    open(REPORT_MD, "w", encoding="utf-8").write(md)
    return md


def render_html(md):
    import html as _h
    body = _h.escape(md)
    html_doc = f"""<!doctype html><meta charset=utf-8><title>PerfCodeGen 复现报告</title>
<style>body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:1100px;margin:30px auto;padding:0 20px;line-height:1.7}}
table{{border-collapse:collapse;width:100%%;font-size:14px}}td,th{{border:1px solid #ccc;padding:6px 10px}}
h1{{border-bottom:2px solid #333;padding-bottom:6px}}h2{{margin-top:30px;color:#1a4480}}
</style><pre style="white-space:pre-wrap;font-family:inherit">{body}</pre>"""
    open(REPORT_HTML, "w", encoding="utf-8").write(html_doc)


if __name__ == "__main__":
    rows = analyze()
    classes = classify_failures(rows)
    md = render_md(rows, classes)
    render_html(md)
    print(md)
    print(f"\n[written] {REPORT_MD}")
    print(f"[written] {REPORT_HTML}")
