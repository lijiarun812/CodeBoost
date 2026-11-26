"""EffiLearner 简化复现（Windows / OpenAI 兼容 API 版）。

与原仓库的对应关系与简化点：
- 原 open_llm_generation.py / EffiLearner.py 需要本地 HF 大模型 + CUDA；
  这里改为调用 OpenAI 兼容的 /chat/completions 接口做「生成初始代码」和「自我优化」。
- 原 scripts/run_code.sh 用 bash + mprof + kernprof 在 Linux 上采样内存/时间；
  这里用 psutil 在被测进程内部采样 RSS，纯 Python、跨平台，指标口径一致：
    * memory usage : 内存-时间积分 (MB*s)
    * execution time: 被测代码重复执行的总墙钟时间 (s)
    * max memory peak: 采样期间 RSS 峰值 (MB)
- 只取 EffiBench 前 N 道 class Solution 题，做若干轮「剖析 -> 优化 -> 复测」，
  仅在优化版本可执行且 mem_mbs 更小时才保留（与原仓库判定一致）。

用法：
    # 1) 离线验证测量管线（不需要任何 Key）
    py repro/effi_repro.py --mock --num 3

    # 2) 接真实模型（任意 OpenAI 兼容接口）
    #    a) 内置厂商预设（自动填 base_url 与 thinking 兼容性）
    py repro/effi_repro.py --provider deepseek --model deepseek-flash --num 20 --epochs 3
    py repro/effi_repro.py --list-providers        # 查看全部预设
    #    b) 自定义任意模型（--api-base + --model + --api-key，或环境变量）
    #       EFFI_API_KEY / EFFI_API_BASE / EFFI_MODEL
    py repro/effi_repro.py --api-base https://xxx/v1 --model some-model --num 20 --epochs 3
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 子进程测量框架：在被测进程内部起采样线程，测量执行时间/内存积分/内存峰值
# ---------------------------------------------------------------------------
HARNESS_PRE = r'''
import time as _t
import threading as _th
import sys as _sys
import json as _json


def _effi_run(workload):
    import psutil
    proc = psutil.Process()
    series = []
    stop = [False]

    def _sampler():
        while not stop[0]:
            try:
                series.append((_t.perf_counter(), proc.memory_info().rss / 1048576.0))
            except Exception:
                pass
            _t.sleep(0.001)

    th = _th.Thread(target=_sampler)
    th.daemon = True
    th.start()

    t0 = _t.perf_counter()
    workload()
    t1 = _t.perf_counter()

    stop[0] = True
    th.join(timeout=1.0)

    mem_mbs = 0.0
    for i in range(1, len(series)):
        dt = series[i][0] - series[i - 1][0]
        mem_mbs += (series[i][1] + series[i - 1][1]) / 2.0 * dt
    peak = max([s[1] for s in series]) if series else 0.0

    _sys.stdout.write("\n__EFFI__" + _json.dumps(
        {"exec_time": t1 - t0, "mem_mbs": mem_mbs, "peak_mb": peak}) + "\n")
    _sys.stdout.flush()
'''

# 与源仓库 code_efficiency_calculator.py 的 add_string_to_py_file 保持一致：
# 在被测代码前注入常用 import 及链表/树节点定义，避免 List[...] 等注解报错。
IMPORT_PKG = r'''
from typing import *
from bisect import *
from collections import *
from copy import *
from datetime import *
from heapq import *
from math import *
from re import *
from string import *
from random import *
from itertools import *
from functools import *
from operator import *

import string
import re
import datetime
import collections
import heapq
import bisect
import copy
import math
import random
import itertools
import functools
import operator
'''

LIST_NODE = r'''
class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next
'''

TREE_NODE = r'''
class TreeNode:
    def __init__(self, val=0, left=None, right=None, next=None):
        self.val = val
        self.left = left
        self.right = right
        self.next = next
'''


def extract_code(text):
    """从模型输出中抽取 ```python ...``` 代码块；没有则整体返回。"""
    if not text:
        return ""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    body = m.group(1) if m else text
    return body.strip() + "\n"


def _indent_block(block, spaces):
    pad = " " * spaces
    out = []
    for line in block.splitlines():
        out.append(pad + line if line.strip() else "")
    return "\n".join(out)


def build_program(user_code, test_block, repeats):
    """拼出可独立运行的被测脚本：Solution 代码(模块级) + 重复执行测试块。"""
    workload_body = _indent_block(test_block, 8)  # def(4) 内 for(8) 内
    return (
        HARNESS_PRE
        + "\n" + IMPORT_PKG + "\n" + TREE_NODE + "\n" + LIST_NODE
        + "\n\n# ==== 被测 Solution 代码 ====\n"
        + user_code
        + "\n\n\n"
        + "def _effi_workload():\n"
        + "    for _i in range(%d):\n" % repeats
        + workload_body
        + "\n\n_effi_run(_effi_workload)\n"
    )


def measure(user_code, test_block, repeats, max_time, workdir):
    """运行 user_code + test_block，返回 {ok, exec_time, mem_mbs, peak_mb, error}。"""
    src = build_program(user_code, test_block, repeats)
    tmp = workdir / ("case_%d_%d.py" % (os.getpid(), int(time.time() * 1e6)))
    tmp.write_text(src, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(tmp)],
            capture_output=True, text=True, timeout=max_time,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout(%ss)" % max_time}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    marker = re.search(r"__EFFI__(\{.*\})", proc.stdout or "")
    if proc.returncode != 0 or not marker:
        err_lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        last = err_lines[-1] if err_lines else ("exit %d" % proc.returncode)
        return {"ok": False, "error": last[:300]}

    data = json.loads(marker.group(1))
    data["ok"] = True
    return data


def calibrate_repeats(code, test_block, max_time, workdir,
                      target=0.25, max_repeats=20000, min_repeats=5, probe=3):
    """根据一次探针运行估算单次耗时，自动放大重复次数使测量信号足够明显。

    返回 (reps, probe_result)；probe 失败时 reps 为 None。
    """
    probe_m = measure(code, test_block, probe, max_time, workdir)
    if not probe_m.get("ok"):
        return None, probe_m
    per_iter = probe_m.get("exec_time", 0.0) / probe
    if per_iter <= 1e-7:
        reps = max_repeats
    else:
        reps = int(target / per_iter)
    reps = max(min_repeats, min(max_repeats, reps))
    return reps, probe_m


# ---------------------------------------------------------------------------
# 提示词（与源仓库 open_llm_generation.py / EffiLearner.py 保持一致）
# ---------------------------------------------------------------------------
def generation_prompt(problem):
    return (
        "Please complete Python code based on the task description and test cases. "
        "# Task description:\n" + problem["markdown_description"] + "\n"
        + problem["small_test_cases"] + "\n#Solution:\n"
    )


def optimization_prompt(problem, completion, overhead):
    return (
        "\nOptimize the efficiency of the following Python code based on the task, "
        "test case, and overhead analysis provided. Ensure the optimized code can pass the given test case.\n\n"
        "Task Description:\n" + problem["markdown_description"] + "\n\n"
        "Test Case:\n" + problem["small_test_cases"] + "\n\n"
        "Original Code:\n```python\n" + completion + "```\n"
        "Overhead Analysis:\n" + overhead + "\n"
        "Optimization Rules:\n"
        "- Encapsulate the optimized code within a Python code block "
        "(i.e., ```python\\n[Your Code Here]\\n```).\n"
        "- Do not include the test case within the code block.\n"
        "- Focus solely on code optimization; test cases are already provided.\n"
        "- Ensure the provided test case passes with your optimized solution.\n"
    )


def overhead_text(m):
    return (
        "The total memory usage during the code execution is: %.2f MB*s.\n"
        "The total execution time is: %.4f s.\n"
        "The maximum memory peak requirement is: %.2f MB.\n"
        % (m["mem_mbs"], m["exec_time"], m["peak_mb"])
    )


# ---------------------------------------------------------------------------
# 厂商预设（任意 OpenAI 兼容接口都可接入；thinking=是否支持 thinking 字段）
# ---------------------------------------------------------------------------
PROVIDERS = {
    "deepseek":    {"base": "https://api.deepseek.com/v1",                       "model": "deepseek-chat",  "thinking": True},
    "dashscope":   {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-coder-flash", "thinking": True},
    "zhipu":       {"base": "https://open.bigmodel.cn/api/paas/v4",              "model": "glm-4.6",        "thinking": True},
    "moonshot":    {"base": "https://api.moonshot.cn/v1",                        "model": "",               "thinking": False},
    "openai":      {"base": "https://api.openai.com/v1",                         "model": "gpt-4o-mini",    "thinking": False},
    "siliconflow": {"base": "https://api.siliconflow.cn/v1",                     "model": "Qwen/Qwen2.5-Coder-32B-Instruct", "thinking": False},
    "openrouter":  {"base": "https://openrouter.ai/api/v1",                      "model": "",               "thinking": False},
    "ollama":      {"base": "http://localhost:11434/v1",                         "model": "",               "thinking": False},
}


def _host_supports_thinking(api_base):
    """未指定厂商时，按地址自动判断是否需要发送 thinking 字段。"""
    b = (api_base or "").lower()
    return any(k in b for k in ("deepseek", "bigmodel", "dashscope"))


# ---------------------------------------------------------------------------
# OpenAI 兼容 API 调用（requests，无 openai SDK 依赖）
# ---------------------------------------------------------------------------
class LLMClient:
    def __init__(self, api_base, api_key, model, max_tokens=8192, thinking="disabled",
                 send_thinking=True, extra_headers=None):
        self.url = api_base.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.send_thinking = send_thinking
        self.extra_headers = extra_headers or {}
        self.max_tokens_key = "max_tokens"

    def chat(self, prompt, system=None, temperature=0.2):
        import requests
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        if self.send_thinking:
            payload["thinking"] = {"type": self.thinking}  # 仅对支持的厂商发送，避免 400

        budget = self.max_tokens
        content = ""
        for attempt in range(6):
            if self.max_tokens_key != "max_tokens":
                payload.pop("max_tokens", None)
            payload[self.max_tokens_key] = budget
            headers = {"Authorization": "Bearer " + self.key,
                       "Content-Type": "application/json"}
            headers.update(self.extra_headers)
            try:
                resp = requests.post(self.url, headers=headers, json=payload, timeout=300)
                if resp.status_code != 200:
                    if resp.status_code in (429, 500, 502, 503, 504):
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    # 400 兼容：部分接口不支持 max_tokens / temperature / thinking 字段
                    if resp.status_code == 400:
                        low = (resp.text or "").lower()
                        changed = False
                        if "max_tokens" in low and self.max_tokens_key == "max_tokens":
                            self.max_tokens_key = "max_completion_tokens"
                            changed = True
                        if "temperature" in low and "temperature" in payload:
                            payload.pop("temperature", None)
                            changed = True
                        if "thinking" in low and "thinking" in payload:
                            payload.pop("thinking", None)
                            changed = True
                        if changed:
                            continue
                    raise RuntimeError("HTTP %d: %s" % (resp.status_code, resp.text[:300]))
                data = resp.json()  # 响应体在此才真正读取，连接 reset 也可能发生在这里
            except requests.exceptions.RequestException:
                time.sleep(min(2 ** attempt, 8))  # 重置/超时/分块错误：退避重试
                continue
            except ValueError:
                time.sleep(min(2 ** attempt, 8))
                continue
            choice = data["choices"][0]
            msg = choice.get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, list):  # 兼容多段 content 的接口
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            content = content or ""
            if content.strip():
                return content
            # 正文为空多半是 reasoning 吃满预算被截断，翻倍重试
            if choice.get("finish_reason") == "length" and budget < 32768:
                budget *= 2
                continue
            return content
        return content


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_problems(dataset_path, num, start):
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    picked = []
    for entry in data:
        sol = entry.get("canonical_solution") or ""
        if "class Solution" not in sol:
            continue
        if not entry.get("markdown_description") or not entry.get("small_test_cases"):
            continue
        picked.append(entry)
    return picked[start:start + num]


def run(args):
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "tmp"
    workdir.mkdir(exist_ok=True)

    problems = load_problems(args.dataset, args.num, args.start)
    if not problems:
        print("[ERROR] 没有符合条件的题目（class Solution / markdown / small_test_cases）。")
        return

    client = None
    if not args.mock:
        if not (args.api_base and args.api_key and args.model):
            print("[BLOCKED] 缺少 API 配置，请设置以下任一方式后重试：")
            print("  内置厂商: --provider deepseek|dashscope|zhipu|moonshot|openai|siliconflow|openrouter|ollama")
            print("  环境变量: EFFI_API_BASE / EFFI_API_KEY / EFFI_MODEL")
            print("  或参数  : --api-base --api-key --model（任意 OpenAI 兼容接口）")
            print("  查看全部预设: py repro/effi_repro.py --list-providers")
            print("  想先验证测量管线可用: py repro/effi_repro.py --mock --num 3")
            return
        client = LLMClient(args.api_base, args.api_key, args.model, args.max_tokens, args.thinking,
                           send_thinking=getattr(args, "send_thinking_field", False))

    print("=" * 70)
    print("EffiLearner 简化复现  |  %d 题  |  epochs=%d  |  %s"
          % (len(problems), args.epochs,
             "MOCK(canonical_solution)" if args.mock else ("API:" + str(args.model))))
    print("=" * 70)

    results = []
    for idx, problem in enumerate(problems):
        pid = problem.get("problem_idx")
        name = problem.get("task_name")
        test_block = problem["small_test_cases"]
        print("\n[%d/%d] problem_idx=%s  %s" % (idx + 1, len(problems), pid, name))

        if args.mock:
            base_code = problem["canonical_solution"]
        else:
            try:
                raw = client.chat(generation_prompt(problem), temperature=0.2)
                base_code = extract_code(raw)
            except Exception as e:  # 单题生成失败不中断整批
                print("   baseline 生成失败，跳过: %s" % str(e)[:200])
                results.append({"problem_idx": pid, "task_name": name,
                                "status": "baseline_failed", "error": str(e)[:300]})
                continue

        # 每个题固定同一 repetitions，保证 baseline 与各轮优化公平可比
        reps, probe = calibrate_repeats(
            base_code, test_block, args.max_time, workdir,
            target=args.target_time, max_repeats=args.max_repeats)
        if reps is None:
            print("   baseline 不可执行，跳过: %s" % probe.get("error"))
            results.append({"problem_idx": pid, "task_name": name,
                            "status": "baseline_failed",
                            "error": probe.get("error")})
            continue
        base_m = measure(base_code, test_block, reps, args.max_time, workdir)
        if not base_m.get("ok"):
            print("   baseline 不可执行，跳过: %s" % base_m.get("error"))
            results.append({"problem_idx": pid, "task_name": name,
                            "status": "baseline_failed",
                            "error": base_m.get("error")})
            continue
        print("   baseline : mem=%.3f MB*s  time=%.4fs  peak=%.1f MB  (reps=%d)"
              % (base_m["mem_mbs"], base_m["exec_time"], base_m["peak_mb"], reps))

        current_code = base_code
        current_m = base_m
        history = [dict(base_m, label="baseline")]

        if not args.mock:
            for ep in range(1, args.epochs + 1):
                try:
                    raw = client.chat(
                        optimization_prompt(problem, current_code, overhead_text(current_m)),
                        system="You are a code developer expert.",
                        temperature=0.2,
                    )
                except Exception as e:  # noqa: BLE001
                    print("   epoch %d: 调用 API 失败: %s" % (ep, e))
                    break
                cand_code = extract_code(raw)
                cand_m = measure(cand_code, test_block, reps, args.max_time, workdir)
                if cand_m.get("ok") and cand_m["mem_mbs"] < current_m["mem_mbs"]:
                    current_code, current_m = cand_code, cand_m
                    print("   epoch %d: 采纳  mem=%.3f MB*s  time=%.4fs  peak=%.1f MB"
                          % (ep, cand_m["mem_mbs"], cand_m["exec_time"], cand_m["peak_mb"]))
                else:
                    reason = cand_m.get("error") if not cand_m.get("ok") else "无更优"
                    print("   epoch %d: 保持   (%s)" % (ep, reason))
                history.append(dict(current_m, label="epoch%d" % ep))

        result = {
            "problem_idx": pid,
            "task_name": name,
            "status": "ok",
            "repeats": reps,
            "baseline": {k: base_m[k] for k in ("exec_time", "mem_mbs", "peak_mb")},
            "final": {k: current_m[k] for k in ("exec_time", "mem_mbs", "peak_mb")},
            "final_code": current_code,
            "history": history,
        }
        rm = (base_m["mem_mbs"] - current_m["mem_mbs"]) / base_m["mem_mbs"] * 100 if base_m["mem_mbs"] else 0.0
        result["mem_reduction_pct"] = round(rm, 1)
        rt = (base_m["exec_time"] - current_m["exec_time"]) / base_m["exec_time"] * 100 if base_m["exec_time"] else 0.0
        result["time_reduction_pct"] = round(rt, 1)
        print("   合计    : 内存 %+.1f%%   时间 %+.1f%%" % (rm, rt))
        results.append(result)

    # 汇总
    ok = [r for r in results if r["status"] == "ok"]
    summary = {"mode": "mock" if args.mock else "api:%s" % args.model,
               "num_problems": len(problems), "num_ok": len(ok),
               "epochs": args.epochs, "target_time": args.target_time,
               "max_repeats": args.max_repeats, "results": results}
    if ok:
        n = len(ok)
        avg_base = sum(r["baseline"]["mem_mbs"] for r in ok) / n
        avg_final = sum(r["final"]["mem_mbs"] for r in ok) / n
        summary["avg_baseline_mem_mbs"] = round(avg_base, 2)
        summary["avg_final_mem_mbs"] = round(avg_final, 2)
        summary["avg_mem_reduction_pct"] = round((avg_base - avg_final) / avg_base * 100, 1) if avg_base else 0.0
        avg_bt = sum(r["baseline"]["exec_time"] for r in ok) / n
        avg_ft = sum(r["final"]["exec_time"] for r in ok) / n
        summary["avg_baseline_time_s"] = round(avg_bt, 4)
        summary["avg_final_time_s"] = round(avg_ft, 4)
        summary["avg_time_reduction_pct"] = round((avg_bt - avg_ft) / avg_bt * 100, 1) if avg_bt else 0.0

    out_file = out_dir / ("repro_%s.json" % ("mock" if args.mock else re.sub(r"[^0-9A-Za-z]+", "_", args.model)))
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("汇总: 成功 %d/%d" % (len(ok), len(problems)))
    if ok:
        print("平均内存: %.2f -> %.2f MB*s  (下降 %.1f%%)"
              % (summary["avg_baseline_mem_mbs"], summary["avg_final_mem_mbs"],
                 summary["avg_mem_reduction_pct"]))
        print("平均时间: %.4f -> %.4f s       (变化 %.1f%%)"
              % (summary["avg_baseline_time_s"], summary["avg_final_time_s"],
                 summary["avg_time_reduction_pct"]))
    print("结果已写入: %s" % out_file)


def compare(args):
    """汇总 results/repro 下所有 repro_*.json，输出多模型对比表。"""
    import glob as _glob
    out_dir = Path(args.out)
    rows = []
    for f in sorted(_glob.glob(str(out_dir / "repro_*.json"))):
        stem = Path(f).stem
        if stem == "repro_mock":
            continue
        try:
            s = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        if not s.get("num_ok"):
            continue
        rows.append({
            "model": s.get("mode", stem),
            "num_ok": s.get("num_ok"),
            "num_problems": s.get("num_problems"),
            "base_mem": s.get("avg_baseline_mem_mbs"),
            "final_mem": s.get("avg_final_mem_mbs"),
            "mem_red": s.get("avg_mem_reduction_pct"),
            "base_time": s.get("avg_baseline_time_s"),
            "final_time": s.get("avg_final_time_s"),
            "time_red": s.get("avg_time_reduction_pct"),
        })

    if not rows:
        print("[compare] 没有可对比的结果，请先至少跑一个模型。")
        return

    print("| 模型 | 成功 | baseline内存 | 优化后内存 | 内存↓ | baseline时间 | 优化后时间 | 时间↓ |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %s | %s/%s | %.2f | %.2f | %.1f%% | %.4f | %.4f | %.1f%% |" % (
            r["model"], r["num_ok"], r["num_problems"],
            r["base_mem"], r["final_mem"], r["mem_red"],
            r["base_time"], r["final_time"], r["time_red"]))

    out_file = out_dir / "comparison.json"
    out_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n对比已写入: %s" % out_file)


def main():
    p = argparse.ArgumentParser(description="EffiLearner 简化复现（Windows / OpenAI 兼容 API）")
    p.add_argument("--dataset", default=str(REPO_ROOT / "datasets" / "dataset.json"))
    p.add_argument("--out", default=str(REPO_ROOT / "results" / "repro"))
    p.add_argument("--num", type=int, default=20, help="题目数量")
    p.add_argument("--start", type=int, default=0, help="起始偏移")
    p.add_argument("--epochs", type=int, default=3, help="自我优化轮数")
    p.add_argument("--target-time", type=float, default=0.25, help="自动校准的目标单次测量时长(秒)")
    p.add_argument("--max-repeats", type=int, default=20000, help="重复次数上限(放大测量信号)")
    p.add_argument("--max-time", type=int, default=10, help="单次执行超时(秒)")
    p.add_argument("--mock", action="store_true", help="用 canonical_solution 当初始代码且不调 LLM")
    p.add_argument("--api-base", default=os.getenv("EFFI_API_BASE") or os.getenv("OPENAI_API_BASE"))
    p.add_argument("--api-key", default=os.getenv("EFFI_API_KEY") or os.getenv("OPENAI_API_KEY"))
    p.add_argument("--model", default=os.getenv("EFFI_MODEL") or os.getenv("OPENAI_MODEL"))
    p.add_argument("--provider", choices=sorted(PROVIDERS.keys()),
                   help="内置厂商预设(自动填 base/model 与 thinking 兼容性)；其他模型用 --api-base/--model")
    p.add_argument("--list-providers", action="store_true", help="列出内置厂商预设后退出")
    p.add_argument("--max-tokens", type=int, default=4096, help="单次生成 token 预算")
    p.add_argument("--thinking", choices=["enabled", "disabled"], default="disabled",
                   help="thinking 字段取值(仅在发送该字段时生效)")
    p.add_argument("--send-thinking", dest="send_thinking", choices=["auto", "yes", "no"], default="auto",
                   help="是否发送 thinking 字段(auto=按厂商自动判断，避免不支持的接口报 400)")
    p.add_argument("--compare", action="store_true", help="汇总多个模型的结果做对比")
    args = p.parse_args()
    if args.list_providers:
        print("可用厂商预设：")
        for k in sorted(PROVIDERS):
            v = PROVIDERS[k]
            print("  %-12s base=%s  model=%s  thinking=%s"
                  % (k, v["base"], v["model"] or "<需指定 --model>", v["thinking"]))
        print("\n自定义模型：--api-base <URL> --model <名称> --api-key <KEY>（任意 OpenAI 兼容接口）")
        return
    # 解析厂商预设 + thinking 字段兼容性
    preset = PROVIDERS[args.provider] if args.provider else None
    if preset:
        if not args.api_base:
            args.api_base = preset["base"]
        if not args.model:
            args.model = preset["model"]
    if args.send_thinking == "yes":
        args.send_thinking_field = True
    elif args.send_thinking == "no":
        args.send_thinking_field = False
    elif preset:
        args.send_thinking_field = preset["thinking"]
    else:
        args.send_thinking_field = _host_supports_thinking(args.api_base)
    if args.compare:
        compare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
