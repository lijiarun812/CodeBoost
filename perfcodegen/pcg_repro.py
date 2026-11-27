#!/usr/bin/env python3
"""PerfCodeGen 轻量复现驱动（base -> correctness refine -> time refine）。

为什么要自己写驱动而不是直接跑官方脚本：
官方流程依赖 ray + 多个 CPU pod + 批量推理接口（见 upstream/README），本机跑不动；
我们把论文的**算法语义**（三相、两类反馈、失败回退）与**提示词模板**
（`upstream/templates_chat.json`，与论文 §2.1/§2.2 一致）保留下来，
用单进程 + 子进程沙盒重写执行层，指标口径与 `eval/metrics.py` 对齐。

与论文的差异（写进汇报第 4 部分）：
1. 单进程执行、串行采样，没有做多 pod 并行；
2. 采样数默认小（论文 sample_num=20），Best@k 因此受 k 限制；
3. 用例切分用 HumanEval `def check(candidate)` 里的每条 assert（论文用数据集自带的
   testcase 输入，并会对大输入做放大）；
4. 时间用“重复执行 + 中位数 + MAD”测，而不是单次 wall clock。

用法：
    python perfcodegen/pcg_repro.py --mock --num 5 --samples 1        # 离线自检
    python perfcodegen/pcg_repro.py --provider deepseek --model deepseek-chat \
        --num 20 --samples 5 --time-strategy feedback
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from eval import metrics  # noqa: E402
from eval.hotlines import hotspots, render as render_hotspots  # noqa: E402
from eval.measure import measure  # noqa: E402
from upstream.sanitize import sanitize  # noqa: E402

TEMPLATES = json.loads(
    (HERE / "upstream" / "templates_chat.json").read_text(encoding="utf-8"))
TRIGGER = TEMPLATES["trigger"].strip()
REGULAR_REQUIREMENT = TEMPLATES["regular_requirement"].strip()

PROVIDERS = {
    "deepseek":    {"base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "dashscope":   {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-coder-flash"},
    "zhipu":       {"base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.6"},
    "moonshot":    {"base": "https://api.moonshot.cn/v1", "model": ""},
    "openai":      {"base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "siliconflow": {"base": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-Coder-32B-Instruct"},
    "ollama":      {"base": "http://localhost:11434/v1", "model": ""},
}

FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)


# ---------------------------------------------------------------------------
# 提示词（模板取自论文仓库，保证与论文的说法一致）
# ---------------------------------------------------------------------------
def base_messages(task):
    return [
        {"role": "system", "content": "You are an excellent code programmer."},
        {"role": "user", "content": task["prompt"] + "\n" + TRIGGER},
    ]


def refine_messages(task, current_code, instruction):
    return base_messages(task) + [
        {"role": "assistant", "content": "```python\n" + current_code + "\n```"},
        {"role": "user", "content": instruction},
    ]


def correctness_instruction(fail):
    """§2.1：把失败用例 / 报错回灌。"""
    case = (fail.get("case_src") or "").strip()
    if fail.get("case") is None or not case:
        return (TEMPLATES["correctness_testcase_feedback"][2]["instruction"]
                .replace("[trigger]", TRIGGER))
    if fail.get("error", "").startswith(("AssertionError", "Assertion")):
        tpl = TEMPLATES["correctness_testcase_feedback"][0]["instruction"]
        return tpl.replace("{testcase}", case).replace("[trigger]", TRIGGER)
    tpl = TEMPLATES["correctness_testcase_feedback"][1]["instruction"]
    return (tpl.replace("{testcase}", case)
            .replace("{error}", fail.get("error", ""))
            .replace("[trigger]", TRIGGER))


def _fmt(value, fmt):
    return fmt % value if isinstance(value, (int, float)) else "n/a"


def _per_case_table(measure_res, limit=5):
    rows = sorted([(i, ms) for i, ms in enumerate(measure_res.get("per_case_ms") or [])],
                  key=lambda x: -x[1])[:limit]
    lines = ["Per-case execution time (slowest first):"]
    for i, ms in rows:
        lines.append("  case #%d: %.3f ms" % (i, ms))
    lines.append("  total: %.3f ms" % (measure_res.get("total_ms") or 0.0))
    return "\n".join(lines)


def _evidence_text(task, code, cur, args):
    """按 --evidence 拼证据包：最慢用例 / per-case 表 / 热点行。"""
    ev = args.evidence or ("all" if args.time_strategy == "fusion" else "slowest")
    slow = cur.get("slowest") or {}
    case_src = (slow.get("src") or "").strip()
    parts = ["Slowest test case:\n%s\nIt costs %.3f ms per execution."
             % (case_src, slow.get("ms") or 0.0)]
    if ev in ("table", "all"):
        parts.append(_per_case_table(cur))
    if ev in ("hotlines", "all"):
        hot = hotspots(code, case_src, task["entry_point"], top=3,
                       max_time=min(10.0, args.max_time), workdir=args.workdir)
        parts.append(render_hotspots(hot))
    return "\n\n".join(parts), ev


def time_instruction(strategy, task, code, cur, args, previous_measure=None):
    """§2.2 的 5 档反馈粒度：对应两条论文路线的不同信号强度。"""
    if strategy == "none":
        return None, "none"
    if strategy == "simple":
        return (TEMPLATES["time_simple_execution_feedback"][0]["instruction"]
                .replace("[regular_requirement]", REGULAR_REQUIREMENT)
                .replace("[trigger]", TRIGGER)), "simple"
    if strategy == "effi3num":
        # EffiLearner 的 result-aware 档：只给 3 个汇总数字，不给用例也不给行级
        overhead = ("The total memory usage during the code execution is: %s MB*s.\n"
                    "The total execution time is: %.4f s.\n"
                    "The maximum memory peak requirement is: %s MB.\n"
                    % (_fmt(cur.get("mem_mbs"), "%.3f"),
                       (cur.get("total_ms") or 0.0) / 1000.0,
                       _fmt(cur.get("peak_mb"), "%.1f")))
        return ((TEMPLATES["time_simple_instruction"][0]["instruction"]
                 .replace("[regular_requirement]", REGULAR_REQUIREMENT)
                 .replace("[trigger]", TRIGGER)) + "\n\nOverhead Analysis:\n" + overhead), "effi3num"
    if strategy == "fusion":
        head = TEMPLATES["time_execution_feedback_with_testcase"][0]["instruction"].replace(
            "[regular_requirement]", REGULAR_REQUIREMENT).replace("[trigger]", TRIGGER)
        ev_text, ev_mode = _evidence_text(task, code, cur, args)
        tail = ("\n\nBefore rewriting, first estimate the time complexity of the current solution, "
                "then implement the optimization. Keep the behaviour identical.")
        return head + "\n\n" + ev_text + tail, ev_mode
    # feedback：PerfCodeGen 的“最慢用例 + 耗时”原始档
    case = (cur.get("slowest") or {}).get("src", "")
    ms = (cur.get("slowest") or {}).get("ms", 0.0)
    head = TEMPLATES["time_execution_feedback_with_testcase"][0]["instruction"].replace(
        "[regular_requirement]", REGULAR_REQUIREMENT).replace("[trigger]", TRIGGER)
    body = TEMPLATES["time_execution_feedback_with_testcase"][1]["instruction"].replace(
        "{testcase}", "%s\n\nIt costs %.3f ms per execution." % (case, ms)).replace(
        "[regular_requirement]", REGULAR_REQUIREMENT).replace("[trigger]", TRIGGER)
    if previous_measure and cur.get("total_ms") and previous_measure.get("total_ms"):
        body += "\n\nFor reference, the previous version took %.3f ms in total." % previous_measure["total_ms"]
    return head + "\n\n" + body, "slowest"


# ---------------------------------------------------------------------------
# LLM 客户端（标准库 urllib，兼容任意 OpenAI 兼容接口）
# ---------------------------------------------------------------------------
class LLM(object):
    def __init__(self, api_base=None, api_key=None, model="", temperature=0.7,
                 max_tokens=4096, timeout=180, mock=False, verbose=False):
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.mock = mock
        self.verbose = verbose
        self.n_calls = 0
        self.mock_reply = None

    def chat(self, messages, retries=3):
        if self.mock:
            self.n_calls += 1
            return [self.mock_reply or ""]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.api_base + "/chat/completions", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key})
        last = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    obj = json.loads(resp.read().decode("utf-8"))
                self.n_calls += 1
                text = obj["choices"][0]["message"].get("content") or ""
                if self.verbose:
                    print("    [llm] %d chars" % len(text))
                return [text]
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError) as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(1.5 * (2 ** attempt))
        raise RuntimeError("LLM 调用失败: %s" % last)


def _parseable(code):
    try:
        compile(code, "<program>", "exec")
        return True
    except SyntaxError:
        return False


def to_program(raw, task):
    """把模型输出变成可执行程序。

    三种情况：① 输出里已含完整函数定义；② 只给了函数体（HumanEval 的常态）；
    ③ 给了带围栏的代码块。统一校验：能编译且确实定义了 entry_point 才算数。
    """
    entry = task["entry_point"]
    want = "def " + entry
    text = (raw or "").replace("\r\n", "\n")
    cands = []

    try:
        code = sanitize(text, entry, chat=True, global_code=True) or ""
        if want in code:
            cands.append(code.strip() + "\n")
    except Exception:  # noqa: BLE001
        pass

    m = FENCE.search(text)
    fenced = m.group(1) if m else text
    if want in fenced:
        cands.append(fenced.strip() + "\n")

    # 只给函数体：保留原有缩进直接拼签名（canonical_solution 自带 4 空格缩进）
    if fenced.strip():
        cands.append(task["prompt"] + "\n" + fenced.rstrip() + "\n")
        dedented = textwrap.dedent(fenced).rstrip()
        reindented = "\n".join(("    " + ln) if ln.strip() else ln
                               for ln in dedented.splitlines())
        cands.append(task["prompt"] + "\n" + reindented + "\n")

    for c in cands:
        if want in c and _parseable(c):
            return c
    return cands[0] if cands else text


# ---------------------------------------------------------------------------
# 单题流水线
# ---------------------------------------------------------------------------
def _measure(code, task, args, runs=None):
    return measure(code, task["test"], task["entry_point"], runs=runs or args.runs,
                   target_time=args.target_time, max_repeats=args.max_repeats,
                   max_time=args.max_time, workdir=args.workdir)


def _accepted(cand, cur, min_gain=0.05):
    """Pareto 判据：时间至少快 min_gain，且（如果有内存数据）常驻内存不劣化超过 5%。"""
    if not cand.get("ok") or not cand.get("total_ms"):
        return False, "wrong"
    if not cand["total_ms"] < cur["total_ms"] * (1.0 - min_gain):
        return False, "no_gain"
    if cand.get("mem_mb_avg") and cur.get("mem_mb_avg"):
        if cand["mem_mb_avg"] > cur["mem_mb_avg"] * 1.05:
            return False, "mem_regression"
    return True, "ok"


def run_task(task, args, llm, out_path):
    t0 = time.time()
    gt = _measure(task["prompt"] + task["canonical_solution"], task, args, runs=max(2, args.runs))
    record = {
        "task_id": task["task_id"],
        "entry_point": task["entry_point"],
        "model": args.model or "mock",
        "n_cases": gt.get("n_cases"),
        "case_mode": gt.get("case_mode"),
        "gt": gt,
    }
    if not gt.get("ok"):
        record["status"] = "gt_failed"
        print("   [跳过] ground truth 跑不动: %s" % gt.get("error"))
        return record

    if gt["total_ms"] < args.min_baseline_ms:
        record["status"] = "noise_skipped"
        print("   [跳过] 基线仅 %.4f ms < %.2f ms，噪声不可控" % (gt["total_ms"], args.min_baseline_ms))
        return record

    # ---- phase 0: base 采样 ----
    samples = []
    for i in range(max(1, args.samples)):
        if args.mock:
            llm.mock_reply = "```python\n" + task["canonical_solution"] + "\n```"
        try:
            raw = llm.chat(base_messages(task))[0]
        except Exception as e:  # noqa: BLE001
            record["status"] = "llm_error"
            record["error"] = str(e)[:300]
            return record
        code = to_program(raw, task)
        m = _measure(code, task, args, runs=1)
        samples.append({"correct": bool(m.get("ok")),
                        "ms": m.get("total_ms") if m.get("ok") else None,
                        "error": None if m.get("ok") else m.get("error"),
                        "code": code})
        if not m.get("ok"):
            samples[-1]["fail"] = m
    record["base"] = {"samples": [{k: v for k, v in s.items() if k not in ("fail", "code")}
                                  for s in samples]}

    ok_samples = [s for s in samples if s.get("correct")]
    if not ok_samples:
        current_code = samples[0].get("code", "")
        current = samples[0].get("fail") or {"ok": False, "error": "no_correct_sample", "case": None}
    else:
        best = min(ok_samples, key=lambda s: s["ms"])
        current_code = best.get("code") or ""
        current = _measure(current_code, task, args, runs=args.runs) if current_code else None

    # ---- phase 1: correctness refine ----
    record["correctness"] = []
    for rd in range(args.correct_rounds):
        if current and current.get("ok"):
            break
        instr = correctness_instruction(current or {"error": "no_correct_sample"})
        try:
            raw = llm.chat(refine_messages(task, current_code, instr))[0]
        except Exception as e:  # noqa: BLE001
            record["correctness"].append({"rd": rd, "accepted": False, "reason": "llm_error", "error": str(e)[:200]})
            break
        code = to_program(raw, task)
        m = _measure(code, task, args, runs=1)
        accepted = bool(m.get("ok"))
        record["correctness"].append({
            "rd": rd, "accepted": accepted,
            "reason": "ok" if accepted else "still_wrong",
            "error": None if accepted else m.get("error"),
            "feedback": instr[:400],
        })
        if accepted:
            current_code = code
            current = _measure(current_code, task, args, runs=args.runs)
            samples.append({"correct": True, "ms": current.get("total_ms")})
            break

    # ---- phase 2: time refine ----
    record["time"] = []
    if current and current.get("ok") and args.time_strategy != "none":
        stale = 0
        for rd in range(args.time_rounds):
            instr, ev_mode = time_instruction(
                args.time_strategy, task, current_code, current, args, previous_measure=None)
            try:
                raw = llm.chat(refine_messages(task, current_code, instr))[0]
            except Exception as e:  # noqa: BLE001
                record["time"].append({"rd": rd, "accepted": False, "reason": "llm_error", "error": str(e)[:200]})
                break
            code = to_program(raw, task)
            cand = _measure(code, task, args, runs=max(2, args.runs))
            ok, reason = _accepted(cand, current, args.min_gain)
            record["time"].append({
                "rd": rd, "accepted": ok, "reason": reason,
                "strategy": args.time_strategy, "evidence": ev_mode,
                "evidence_chars": len(instr or ""),
                "evidence_pack": (instr or "")[:800],
                "before_ms": current.get("total_ms"),
                "after_ms": cand.get("total_ms") if cand.get("ok") else None,
                "slowest": (current.get("slowest") or {}).get("src", "")[:200],
            })
            if ok:
                current_code, current = code, cand
                stale = 0
            else:
                stale += 1
                if stale >= max(1, args.patience):
                    print("   [早停] 连续 %d 轮无收益（min_gain=%.0f%%）"
                          % (stale, args.min_gain * 100))
                    break

    # ---- final ----
    if current and current.get("ok"):
        final = _measure(current_code, task, args, runs=max(2, args.runs))
        if not final.get("ok"):
            final = current
        record["final"] = {
            "correct": True,
            "ms": final.get("total_ms"),
            "speedup": round(metrics.speedup(gt["total_ms"], final.get("total_ms")), 4),
            "opt": metrics.is_opt(gt["total_ms"], final.get("total_ms")),
            "mem_mb_avg": final.get("mem_mb_avg"),
            "rel_mad": final.get("rel_mad"),
            "code": current_code,
        }
        record["status"] = "ok"
    else:
        record["final"] = {"correct": False, "ms": None, "speedup": 0.0, "opt": False}
        record["status"] = "failed"

    record["rel_mad"] = (record["final"] or {}).get("rel_mad") or gt.get("rel_mad")
    record["n_calls"] = llm.n_calls
    record["wall_s"] = round(time.time() - t0, 2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(records):
    tasks = []
    for r in records:
        if r.get("status") not in ("ok", "failed"):
            continue
        samples = (r.get("base") or {}).get("samples") or []
        tasks.append({
            "task_id": r["task_id"],
            "gt_ms": (r.get("gt") or {}).get("total_ms"),
            "samples": [{"correct": s.get("correct"), "ms": s.get("ms")} for s in samples],
            "final": {"correct": (r.get("final") or {}).get("correct"),
                      "ms": (r.get("final") or {}).get("ms")},
            "rel_mad": r.get("rel_mad"),
        })
    return metrics.aggregate(tasks)


def main(argv=None):
    p = argparse.ArgumentParser(description="PerfCodeGen 轻量复现驱动")
    p.add_argument("--dataset", default=str(ROOT / "datasets" / "sample_humaneval_20.json"))
    p.add_argument("--num", type=int, default=10)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--samples", type=int, default=1, help="base 阶段采样数（论文 20）")
    p.add_argument("--correct-rounds", type=int, default=1)
    p.add_argument("--time-rounds", type=int, default=1)
    p.add_argument("--time-strategy", choices=["feedback", "simple", "none", "effi3num", "fusion"],
                   default="feedback",
                   help="反馈粒度：none 不优化 / simple 只说优化 / feedback 最慢用例 / "
                        "effi3num EffiLearner 的 3 数字档 / fusion 最慢用例+per-case 表+热点行")
    p.add_argument("--evidence", choices=["", "slowest", "table", "hotlines", "all"], default="",
                   help="fusion 的证据包内容（默认 fusion→all，其余→slowest）")
    p.add_argument("--min-gain", type=float, default=0.05, help="判定“有收益”的最小时间降幅")
    p.add_argument("--patience", type=int, default=1, help="连续几轮无收益后早停")
    p.add_argument("--model", default="")
    p.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", ""))
    p.add_argument("--api-key", default=os.environ.get("OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    p.add_argument("--provider", default="", choices=[""] + sorted(PROVIDERS))
    p.add_argument("--list-providers", action="store_true")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--runs", type=int, default=3, help="最终测量取样次数（中位数）")
    p.add_argument("--target-time", type=float, default=0.25)
    p.add_argument("--max-repeats", type=int, default=20000)
    p.add_argument("--max-time", type=float, default=20.0)
    p.add_argument("--min-baseline-ms", type=float, default=0.0,
                   help="基线耗时低于该值的题直接跳过（建议 1.0，避开亚毫秒噪声）")
    p.add_argument("--max-calls", type=int, default=200)
    p.add_argument("--out", default=str(ROOT / "results" / "perfcodegen"))
    p.add_argument("--resume", action="store_true")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.list_providers:
        for k, v in sorted(PROVIDERS.items()):
            print("%-12s %s  (默认模型: %s)" % (k, v["base"], v["model"] or "-"))
        return 0

    if args.provider:
        preset = PROVIDERS[args.provider]
        args.api_base = args.api_base or preset["base"]
        args.model = args.model or preset["model"]

    if not args.mock and (not args.api_base or not args.api_key):
        print("缺少 --api-base / --api-key（或环境变量 OPENAI_BASE_URL / OPENAI_KEY）。"
              "可先用 --mock 自检。", file=sys.stderr)
        return 2

    workdir = Path(args.out) / "_tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    args.workdir = str(workdir)

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    tasks = data[args.start:args.start + args.num]
    if not tasks:
        print("没有题目可跑（--dataset=%s，--start=%d）" % (args.dataset, args.start), file=sys.stderr)
        return 2

    model_slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", args.model or "mock")
    out_dir = Path(args.out) / model_slug
    out_dir.mkdir(parents=True, exist_ok=True)

    llm = LLM(api_base=args.api_base, api_key=args.api_key, model=args.model,
              temperature=args.temperature, max_tokens=args.max_tokens,
              mock=args.mock, verbose=args.verbose)

    records = []
    t0 = time.time()
    for i, task in enumerate(tasks, 1):
        slug = task["task_id"].replace("/", "_")
        path = out_dir / (slug + ".json")
        if args.resume and path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            print("[%d/%d] %s  (resume)" % (i, len(tasks), task["task_id"]))
            records.append(rec)
            continue
        print("[%d/%d] %s" % (i, len(tasks), task["task_id"]))
        if llm.n_calls >= args.max_calls:
            print("达到 --max-calls=%d，提前停止并保存汇总。" % args.max_calls, file=sys.stderr)
            break
        try:
            rec = run_task(task, args, llm, path)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            rec = {"task_id": task["task_id"], "status": "error", "error": str(e)[:300]}
            print("   出错: %s" % str(e)[:200])
        records.append(rec)
        if rec.get("status") == "ok":
            print("   gt=%.2fms  final=%.2fms  speedup=%.2fx  calls=%d"
                  % (rec["gt"]["total_ms"], rec["final"]["ms"] or 0,
                     rec["final"]["speedup"], llm.n_calls))

    summary = summarize(records)
    summary.update({
        "model": args.model or "mock",
        "dataset": str(args.dataset),
        "num_requested": len(tasks),
        "samples": args.samples,
        "correct_rounds": args.correct_rounds,
        "time_rounds": args.time_rounds,
        "time_strategy": args.time_strategy,
        "evidence": args.evidence or ("all" if args.time_strategy == "fusion" else "slowest"),
        "min_gain": args.min_gain,
        "patience": args.patience,
        "temperature": args.temperature,
        "min_baseline_ms": args.min_baseline_ms,
        "n_calls": llm.n_calls,
        "wall_s": round(time.time() - t0, 1),
        "skipped": [r["task_id"] for r in records if r.get("status") in ("noise_skipped", "gt_failed")],
    })
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n" + metrics.format_summary_table(summary, title="PerfCodeGen 复现（%s）" % (args.model or "mock")))
    print("\n结果目录: %s" % out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

