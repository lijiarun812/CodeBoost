#!/usr/bin/env python3
"""统一测量 harness：全组唯一的“尺子”（对应 docs/优化计划.md 第 8.2 节）。

设计要点（与中期旧尺子的差别）：
1. **估计量**：每档重复 E=12 批，去掉最高/最低各 2 个后取均值；报告 median/IQR/CV。
2. **双口径内存**：`peak_tracemalloc_mb`（Python 分配峰值，可追到行）与
   `peak_rss_mb`（整个进程实际占用峰值）分开报，**不再使用 RSS-时间积分**。
3. **心跳看门狗**：子进程每 5s 打一次心跳，父进程 30s 收不到才判定卡死；
   卡死不再等于“答错”，而是独立的 `timeout` 状态。
4. **降档重试**：某档超时就把重复次数降到 1/4 重跑一次，并把 `downgraded` 记进结果。
5. **五类状态**：ok / incorrect / timeout / resource_exceeded / api_failed。

对外的两个入口：
    measure_task(task, ...)   -> Result（全字段，可直接落盘）
    calibrate_task(task)      -> difficulty 片段（baseline_ms / cv / category），供 B 回填题库

CLI 用法：
    python eval/harness.py --dataset humaneval --tasks 31,40,96 --code canonical
    python eval/harness.py --dataset effibench --tasks 3 --code none --code-file cand.py
    python eval/harness.py --selftest

注意：`code` 是“候选代码”，`--code canonical` 表示用数据集自带标准解（标定/体检用）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import hashlib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # 作为脚本运行 / 作为包导入两种方式都能拿到 inputs
    from eval import inputs as _inputs
except Exception:  # noqa: BLE001
    try:
        import inputs as _inputs
    except Exception:  # noqa: BLE001
        _inputs = None

PROTOCOL_VERSION = "proto-2026.11"
E_REPS = 12
TRIM = 2
TARGET_BATCH_S = 0.25
MAX_REPS = 200000
MIN_REPS = 1
HEARTBEAT_S = 5.0
STALE_S = 30.0
DEFAULT_MAX_MEM_MB = 2048.0

# EffiBench 的 canonical solution 依赖这些常用导入与节点定义（与官方
# code_efficiency_calculator.py 的 add_string_to_py_file 一致），测试块本身不 import。
PREAMBLE = r'''
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
import string, re, datetime, collections, heapq, math, bisect


class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

    def __repr__(self):
        return "ListNode(%r)" % (self.val,)


class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

    def __repr__(self):
        return "TreeNode(%r)" % (self.val,)
'''

# ---------------------------------------------------------------------------
# 子进程程序模板：心跳 + 双口径内存 + 校验 + 批量计时
# ---------------------------------------------------------------------------
_CHILD = r'''# -*- coding: utf-8 -*-
import json, sys, threading, time

try:
    import tracemalloc
except Exception:  # pragma: no cover
    tracemalloc = None
try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

CODE = __CODE__
TEST = __TEST__
KIND = __KIND__
ENTRY = __ENTRY__
CASES = __CASES__
REPS = __REPS__
MODE = __MODE__
MAX_MEM = __MAX_MEM__


def hb(**kw):
    kw["t"] = time.time()
    sys.stdout.write("__HB__" + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit(obj):
    sys.stdout.write("__RESULT__" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_namespace():
    g = {"__name__": "__harness__"}
    return g


def main():
    hb(stage="start")
    g = build_namespace()
    try:
        exec(compile(CODE, "<solution>", "exec"), g)
    except Exception as e:
        emit({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
        return

    ns = dict(g)

    def bind_entry():
        """把 ENTRY 绑成「可调用的对象」，并补齐 EffiBench 的实例名约定。

        背景：EffiBench 的测试块写的是 `assert solution.xxx(...)`（**实例**），
        而代码里定义的是 `class Solution`；ENTRY 常被传成 "solution"。
        旧实现只绑 `candidate`，于是 test_block 下测试块必然 NameError，
        上层再把异常吞掉 → 优化线的 F2/F3/F4 会静默退化成 F1。
        """
        obj = ns.get(ENTRY) or g.get(ENTRY)
        if obj is None and isinstance(ns.get(ENTRY[:1].upper() + ENTRY[1:]), type):
            obj = ns.get(ENTRY[:1].upper() + ENTRY[1:])   # solution -> Solution
        if obj is None:
            # 找不到入口不算错：kind="assert" 的测试块可能直接引用自己的函数名，
            # test_block 里也可能自带 `solution = Solution()`。留给测试自己报错。
            return None
        inst = obj() if isinstance(obj, type) else obj
        ns.setdefault(ENTRY, inst)
        ns.setdefault("solution", inst)
        ns.setdefault("candidate", inst)
        return inst

    if MODE == "cases":
        # 逐用例诊断：与主口径**同一条计时路径**（不开 tracemalloc、包函数调用），
        # 只是把整块拆成一条条用例分别计时。用于“最慢用例/热点区域”的反馈构造。
        fns = []
        hb(stage="check", mode="cases")
        try:
            bind_entry()
            for i, src in enumerate(CASES):
                w = "def _case_%d():\n" % i + "\n".join(
                    ("    " + ln) if ln.strip() else ln for ln in src.splitlines())
                exec(compile(w, "<case%d>" % i, "exec"), ns)
                fns.append(ns["_case_%d" % i])
            for f in fns:
                f()
        except Exception as e:
            emit({"ok": False, "status": "incorrect",
                  "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
            return
        out = []
        for i, f in enumerate(fns):
            t0 = time.perf_counter()
            for _ in range(REPS):
                f()
            out.append((time.perf_counter() - t0) / REPS * 1000.0)
            hb(stage="case", done=i + 1, of=len(fns))
        emit({"ok": True, "mode": "cases", "reps": REPS, "cases_ms": out,
              "total_ms": sum(out)})
        return

    if KIND == "humaneval":
        hb(stage="check")
        try:
            exec(compile(TEST, "<test>", "exec"), ns)
            check = ns.get("check")
            cand = ns.get(ENTRY) or g.get(ENTRY)
            if check is None or cand is None:
                raise NameError("missing check()/entry_point=%s" % ENTRY)
            check(cand)
        except Exception as e:
            emit({"ok": False, "status": "incorrect",
                  "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
            return

        def run_unit():
            check(cand)
    else:
        # 关键：把测试块包成函数再调用，而不是每次 exec(代码对象)。
        # 否则 exec 的建帧/字典查找开销会盖过真实计算（小用例上能放大 5–10 倍）。
        wrapped = "def _codeboost_unit():\n" + "\n".join(
            ("    " + ln) if ln.strip() else ln for ln in TEST.splitlines())
        hb(stage="check")
        try:
            bind_entry()
            exec(compile(wrapped, "<test>", "exec"), ns)
            unit_fn = ns["_codeboost_unit"]
            unit_fn()
        except Exception as e:
            emit({"ok": False, "status": "incorrect",
                  "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
            return

        def run_unit():
            unit_fn()

    hb(stage="warmup")
    run_unit()

    rss_peak = [0.0]
    stop = [False]

    def sampler():
        if psutil is None:
            return
        p = psutil.Process()
        while not stop[0]:
            try:
                rss_peak[0] = max(rss_peak[0], p.memory_info().rss / 1048576.0)
            except Exception:
                pass
            time.sleep(0.005)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    # 关键：tracemalloc 会为每次分配插桩，实测能把小用例放慢 3–6 倍。
    # 所以时间与内存分两次测：mode="time" 不开 tracemalloc（干净计时），
    # mode="mem" 只取分配峰值（不计时）。
    if MODE == "mem" and tracemalloc is not None:
        tracemalloc.start()
    hb(stage="run", reps=REPS)
    step = max(1, REPS // 20)
    t0 = time.perf_counter()
    for i in range(REPS):
        run_unit()
        if i % step == 0:
            hb(stage="run", done=i, reps=REPS)
    elapsed = time.perf_counter() - t0
    peak_tm = None
    if MODE == "mem" and tracemalloc is not None:
        _, peak_tm = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    stop[0] = True
    th.join(timeout=0.5)
    emit({
        "ok": True,
        "mode": MODE,
        "batch_s": elapsed,
        "reps": REPS,
        "peak_tracemalloc_mb": (peak_tm / 1048576.0) if peak_tm is not None else None,
        "peak_rss_mb": rss_peak[0] or None,
        "over_max_mem": bool(rss_peak[0] and rss_peak[0] > MAX_MEM),
    })


main()
'''


# ---------------------------------------------------------------------------
# 统计与工具
# ---------------------------------------------------------------------------
def trimmed_mean(values, trim=TRIM):
    """去掉最高/最低各 trim 个后取均值；样本不足时退化为普通均值。"""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) <= 2 * trim:
        return statistics.mean(vals)
    kept = vals[trim:len(vals) - trim]
    return statistics.mean(kept)


def describe(values):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "iqr": None, "cv": None, "min": None, "max": None}
    med = statistics.median(vals)
    if len(vals) >= 4:
        q1 = statistics.median(vals[:len(vals) // 2])
        q3 = statistics.median(vals[(len(vals) + 1) // 2:])
        iqr = q3 - q1
    else:
        iqr = max(vals) - min(vals)
    mean = statistics.mean(vals)
    cv = (statistics.pstdev(vals) / mean) if (len(vals) > 1 and mean) else 0.0
    return {"n": len(vals), "median": round(med, 4), "iqr": round(iqr, 4),
            "cv": round(cv, 4), "min": round(min(vals), 4), "max": round(max(vals), 4)}


def dataset_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()[:16]


def harness_commit():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def manifest(model="", model_version="", temperature=0.2, seed=42, dataset_path=None):
    return {
        "model": model,
        "model_version": model_version or model,
        "temperature": temperature,
        "seed": seed,
        "dataset_hash": dataset_hash(dataset_path) if dataset_path else "n/a",
        "harness_commit": harness_commit(),
        "protocol_version": PROTOCOL_VERSION,
    }


# ---------------------------------------------------------------------------
# 子进程执行 + 心跳看门狗
# ---------------------------------------------------------------------------
def _build_child(code, test, kind, reps, max_mem, entry="solution", mode="time", cases=None):
    if kind == "test_block":  # EffiBench：补上官方那套 import 前缀
        code = PREAMBLE + "\n" + code
    return (_CHILD
            .replace("__CODE__", repr(code))
            .replace("__TEST__", repr(test))
            .replace("__KIND__", repr(kind))
            .replace("__ENTRY__", repr(entry))
            .replace("__MODE__", repr(mode))
            .replace("__CASES__", repr(cases or []))
            .replace("__REPS__", str(int(reps)))
            .replace("__MAX_MEM__", repr(float(max_mem))))


class _Run(object):
    def __init__(self):
        self.result = None
        self.last_hb = time.time()
        self.status = None
        self.error = None
        self.killed = False


_SCRIPT_PREFIX = "hb_"


def _purge_scripts(script_dir, max_age_s=3600):
    """清理遗留的临时脚本（沙箱会把删除重定向到回收站，攒多了很脏）。"""
    try:
        now = time.time()
        for p in Path(script_dir).glob(_SCRIPT_PREFIX + "*.py"):
            try:
                if now - p.stat().st_mtime > max_age_s:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def run_child(program, stale_s=STALE_S, budget_s=60.0, workdir=None):
    """跑一个子进程，返回 _Run（含 result / status / error / killed）。

    脚本**一律写系统 temp**（绝对路径），workdir 只作为子进程 cwd：
    这样 workdir 是相对路径也不会找不到脚本，也不会把 results/ 塞满临时文件。
    """
    script_dir = Path(tempfile.gettempdir()).resolve()
    wd = Path(workdir).resolve() if workdir else script_dir
    try:
        wd.mkdir(parents=True, exist_ok=True)
    except OSError:
        wd = script_dir
    _purge_scripts(script_dir)
    path = script_dir / ("%s%d_%d.py" % (_SCRIPT_PREFIX, os.getpid(), int(time.time() * 1e6)))
    path.write_text(program, encoding="utf-8")

    run = _Run()
    proc = subprocess.Popen([sys.executable, str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", cwd=str(wd))
    lock = threading.Lock()

    def _reader():
        for line in proc.stdout:
            if line.startswith("__HB__"):
                with lock:
                    run.last_hb = time.time()
            elif line.startswith("__RESULT__"):
                try:
                    with lock:
                        run.result = json.loads(line[len("__RESULT__"):])
                except Exception:  # noqa: BLE001
                    with lock:
                        run.error = "bad result payload"
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    stderr_buf = []

    def _err_reader():
        # stderr 必须**并发**读：被测代码一打印多，64KB 管道塞满，
        # 子进程会阻塞在 write 上、心跳停发，于是被看门狗误判 timeout。
        try:
            for line in proc.stderr:
                stderr_buf.append(line)
        except Exception:  # noqa: BLE001
            pass

    err_thread = threading.Thread(target=_err_reader, daemon=True)
    err_thread.start()
    t0 = time.time()
    while True:
        if proc.poll() is not None:
            break
        now = time.time()
        with lock:
            last = run.last_hb
        if now - last > stale_s:
            run.killed = True
            run.status = "timeout"
            run.error = "heartbeat stale %.1fs" % stale_s
            proc.kill()
            break
        if now - t0 > budget_s:
            run.killed = True
            run.status = "timeout"
            run.error = "budget exceeded %.1fs" % budget_s
            proc.kill()
            break
        time.sleep(0.2)
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        proc.kill()
    reader.join(timeout=2)
    err_thread.join(timeout=2)
    stderr = "".join(stderr_buf)
    try:
        path.unlink()
    except OSError:
        pass

    if run.result is None and run.status is None:
        tail = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        run.status = "incorrect"
        run.error = tail[-1][:300] if tail else "no result (exit %s)" % proc.returncode
    return run


# ---------------------------------------------------------------------------
# 标定 + 测量
# ---------------------------------------------------------------------------
def calibrate(code, test, kind="assert", target_batch_s=TARGET_BATCH_S,
              max_reps=MAX_REPS, max_mem=DEFAULT_MAX_MEM_MB, workdir=None,
              entry="solution"):
    """用 5 次重复探针估算单次耗时，放大到 target_batch_s。"""
    probe = run_child(_build_child(code, test, kind, 1, max_mem, entry),
                      stale_s=STALE_S, budget_s=60.0, workdir=workdir)
    if probe.result is None or not probe.result.get("ok"):
        return None, None, probe
    per_exec = probe.result["batch_s"] / 1.0
    if per_exec < 1e-3:  # 太小的用例用 50 次探针提高估计精度
        probe2 = run_child(_build_child(code, test, kind, 50, max_mem, entry),
                           stale_s=STALE_S, budget_s=60.0, workdir=workdir)
        if probe2.result and probe2.result.get("ok"):
            per_exec = probe2.result["batch_s"] / 50.0
    if per_exec <= 1e-9:
        reps = max_reps
    else:
        reps = int(target_batch_s / per_exec)
    reps = max(MIN_REPS, min(max_reps, reps))
    return reps, per_exec, probe


def measure_code(code, test, kind="assert", e_reps=E_REPS, trim=TRIM,
                 target_batch_s=TARGET_BATCH_S, max_reps=MAX_REPS,
                 max_mem=DEFAULT_MAX_MEM_MB, stale_s=STALE_S, workdir=None,
                 progress=None, entry="solution"):
    """按 E=12 去极值测一份代码：返回 dict（含 status/time/mem）。"""
    reps, per_exec, probe = calibrate(code, test, kind, target_batch_s, max_reps, max_mem,
                                      workdir, entry)
    if reps is None:
        res = probe.result or {}
        return {"status": probe.status or "incorrect",
                "error": probe.error or res.get("error"), "down": False}

    stale_floor = stale_s if stale_s else STALE_S
    # 预算必须按“这一批真正要跑多久”算，而不是固定 5s：
    # 单次 2s 的题 reps=1 也要 2s，固定 5s 会把它判 timeout（老师第 4 条）。
    expected_batch_s = max(target_batch_s, reps * (per_exec or 0.0))
    budget = max(10.0, 20.0 * expected_batch_s)
    values, rss = [], []
    downgraded = False
    for i in range(e_reps):
        run = run_child(_build_child(code, test, kind, reps, max_mem, entry),
                        stale_s=stale_floor, budget_s=budget, workdir=workdir)
        if run.result is None or not run.result.get("ok"):
            if run.status == "timeout" and not downgraded:
                # 降档重试：重复次数砍到 1/4，仍超时就认输
                downgraded = True
                reps = max(MIN_REPS, reps // 4)
                run = run_child(_build_child(code, test, kind, reps, max_mem, entry),
                                stale_s=stale_floor, budget_s=budget, workdir=workdir)
                if run.result is None or not run.result.get("ok"):
                    return {"status": "timeout", "error": run.error,
                            "down": True, "downgraded": True, "reps": reps}
            else:
                if run.status == "timeout":
                    return {"status": "timeout", "error": run.error,
                            "down": True, "downgraded": True, "reps": reps}
                res = run.result or {}
                return {"status": "incorrect",
                        "error": res.get("error") or run.error, "down": False, "reps": reps}
        r = run.result
        values.append(r["batch_s"] / float(r["reps"]) * 1000.0)
        if r.get("peak_rss_mb") is not None:
            rss.append(r["peak_rss_mb"])
        if r.get("over_max_mem"):
            return {"status": "resource_exceeded",
                    "error": "rss > %.0f MB" % max_mem, "down": True, "reps": reps}
        if progress:
            progress(i + 1, e_reps)

    # 内存单独测一次：tracemalloc 插桩会显著拖慢执行，绝不能在计时的那几批里开着
    peak_tm = None
    mem_run = run_child(_build_child(code, test, kind, max(1, min(reps, 5)), max_mem, entry, "mem"),
                        stale_s=stale_floor, budget_s=budget, workdir=workdir)
    if mem_run.result and mem_run.result.get("ok"):
        peak_tm = mem_run.result.get("peak_tracemalloc_mb")
        if mem_run.result.get("peak_rss_mb") is not None:
            rss.append(mem_run.result["peak_rss_mb"])
        if mem_run.result.get("over_max_mem"):
            return {"status": "resource_exceeded",
                    "error": "rss > %.0f MB" % max_mem, "down": True, "reps": reps}

    desc = describe(values)
    mem = {
        "peak_tracemalloc_mb": round(peak_tm, 4) if peak_tm is not None else None,
        "peak_rss_mb": round(max(rss), 3) if rss else None,
    }
    return {
        "status": "ok",
        # median 是辅口径（真中位数），主口径是去极值均值——两者不能再是同一个值
        "median_ms": round(statistics.median(values), 4),
        "mean_trimmed_ms": trimmed_mean(values, trim),
        "raw_ms": [round(v, 4) for v in values],
        "stats": desc,
        "reps": reps,
        "downgraded": downgraded,
        "mem": mem,
    }


# ---------------------------------------------------------------------------
# 数据集适配（只做 harness 需要的两种，B 会在此基础上扩展四层）
# ---------------------------------------------------------------------------
def load_tasks(dataset, path, ids=None, limit=None):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("数据集应为 JSON 数组: %s" % path)
    tasks = []
    if dataset == "effibench":  # 1000 条，class Solution + small_test_cases
        for item in data:
            idx = str(item.get("problem_idx"))
            if ids and idx not in ids:
                continue
            tasks.append({
                "task_id": "EffiBench/%s" % idx,
                "dataset": "effibench",
                "kind": "test_block",
                "task_name": item.get("task_name", ""),
                "code": item.get("canonical_solution", ""),
                "test": item.get("small_test_cases", ""),
                "generator": item.get("test_case_generator", ""),
                "entry_point": "solution",
            })
    else:  # HumanEval 小样：prompt + canonical_solution + def check(candidate)
        for item in data:
            tid = item.get("task_id", "")
            short = tid.split("/")[-1]
            if ids and short not in ids and tid not in ids:
                continue
            tasks.append({
                "task_id": tid,
                "dataset": "humaneval",
                "kind": "humaneval",
                "task_name": item.get("entry_point", ""),
                "code": item.get("prompt", "") + item.get("canonical_solution", ""),
                "test": item.get("test", ""),
                "entry_point": item.get("entry_point", "solution"),
            })
    if limit:
        tasks = tasks[:limit]
    return tasks


# ---------------------------------------------------------------------------
# 对外主接口
# ---------------------------------------------------------------------------
_CHECK_DEF = re.compile(r"^(\s*)def\s+check\s*\(")


def split_cases(test_code):
    """把 `def check(candidate)` 拆成一条条 assert（拆不动就整块当一条）。

    与旧 eval/measure.py 的切分规则一致，但**计时路径改用 harness 自己的**，
    避免“反馈用旧尺子、报告用新尺子”。
    """
    lines = (test_code or "").splitlines()
    start, indent = None, 0
    for i, ln in enumerate(lines):
        m = _CHECK_DEF.match(ln)
        if m:
            start, indent = i, len(m.group(1))
            break
    if start is None:
        body = textwrap.dedent(test_code or "").strip()
        return ([body], "block") if body else ([], "block")
    body, body_indent = [], None
    for ln in lines[start + 1:]:
        if not ln.strip():
            continue
        cur = len(ln) - len(ln.lstrip())
        if cur <= indent:
            break
        body_indent = cur if body_indent is None else body_indent
        body.append(ln)
    cases, cur_case, has_other = [], [], False
    for ln in body:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        at_top = (len(ln) - len(ln.lstrip())) == body_indent
        if at_top and s.startswith("assert"):
            if cur_case:
                cases.append(cur_case)
            cur_case = [ln]
        elif at_top:
            has_other = True
            cur_case.append(ln)
        else:
            cur_case.append(ln)
    if cur_case:
        cases.append(cur_case)
    if has_other or not cases:
        block = textwrap.dedent("\n".join(body)).strip()
        return ([block], "block") if block else ([], "block")
    return ([textwrap.dedent("\n".join(c)).strip() for c in cases], "split")


def measure_cases(code, test, kind="assert", entry="solution", case_reps=5,
                  target_case_s=0.25, max_mem=DEFAULT_MAX_MEM_MB, workdir=None,
                  stale_s=STALE_S):
    """逐用例计时（同一把尺子）：返回每条的 ms、最慢用例与源码。

    计时路径与 measure_code 完全一致：子进程隔离、不开 tracemalloc、包函数调用。
    """
    cases, mode = split_cases(test)
    if not cases:
        return {"status": "incorrect", "error": "no_cases", "mode": mode}

    def _run(reps):
        return run_child(_build_child(code, test, kind, reps, max_mem, entry, "cases", cases),
                         stale_s=stale_s, budget_s=max(30.0, 20.0 * reps * 0.001),
                         workdir=workdir)

    run = _run(case_reps)
    if run.result is None or not run.result.get("ok"):
        err = (run.result or {}).get("error") or run.error or "cases_failed"
        return {"status": run.status or (run.result or {}).get("status") or "incorrect",
                "error": err, "mode": mode}
    ms = list(run.result.get("cases_ms") or [])
    reps = run.result.get("reps") or case_reps
    fastest = min(ms) if ms else 0.0
    if fastest and fastest < 1.0:  # 太小的用例再放大一轮，保证排序可信
        slow = max(ms) / 1000.0
        reps2 = max(1, min(MAX_REPS, int(target_case_s / slow))) if slow > 0 else case_reps
        if reps2 > case_reps * 2:
            run2 = _run(reps2)
            if run2.result and run2.result.get("ok"):
                run, ms, reps = run2, list(run2.result.get("cases_ms") or []), reps2
    items = [{"idx": i, "ms": round(float(v), 4), "src": cases[i][:300]}
             for i, v in enumerate(ms)]
    items.sort(key=lambda x: -x["ms"])
    best = items[0] if items else {"idx": None, "ms": 0.0}
    return {
        "status": "ok",
        "mode": mode,
        "reps": reps,
        "cases": items,
        "total_ms": round(sum(ms), 4),
        "slowest": {"idx": best["idx"], "ms": best["ms"],
                    "src": cases[best["idx"]] if best["idx"] is not None else ""},
    }

def measure_task(task, code=None, method="base", dataset_path=None,
                 tier_plan=None, min_ms=2.0, model="", model_version="",
                 temperature=0.2, seed=42, **kw):
    """测一个 Task，返回符合 schema 的 Result。

    tier_plan: 由 eval/inputs.py 给出的档位列表；给多档时按 min_ms 自动升档，
    每次尝试都记进 time.tiers，避免“只用一档就下结论”。
    """
    use_code = code if code is not None else task["code"]
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", model or "canonical")
    run_id = "%s-%s-s%d-%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), slug, seed,
                                  harness_commit(), task["task_id"].replace("/", "_"))
    started = datetime.now().isoformat(timespec="seconds")
    plan = tier_plan or [{"tier": "t1", "n": None, "gen": "native",
                          "args_hash": None, "test": task["test"]}]
    attempts, final = [], None
    for i, cand in enumerate(plan):
        m = measure_code(use_code, cand["test"], kind=task.get("kind", "assert"),
                         entry=task.get("entry_point", "solution"), **kw)
        final = m
        attempts.append({
            "tier": cand.get("tier"),
            "n": cand.get("n"),
            "gen": cand.get("gen"),
            "args_hash": cand.get("args_hash"),
            "status": m.get("status"),
            "median_ms": round(m["median_ms"], 4) if m.get("status") == "ok" else None,
            "iqr": m["stats"]["iqr"] if m.get("status") == "ok" else None,
            "cv": m["stats"]["cv"] if m.get("status") == "ok" else None,
            "reps": m.get("reps"),
            "downgraded": m.get("downgraded", False),
        })
        if m.get("status") != "ok":
            # native 测试块本身可能有缺陷（EffiBench/100 就是把 list 传给 TreeNode），
            # 这种情况下换生成档再试一次是有意义的；真改错的代码在生成档照样会失败。
            if m.get("status") == "incorrect" and i < len(plan) - 1:
                continue
            break
        too_fast = (m.get("mean_trimmed_ms") or m["median_ms"]) < min_ms
        if too_fast and i < len(plan) - 1:
            continue  # 升档
        break

    m = final
    status = m.get("status", "ok")
    if status == "ok":
        time_block = {
            "median_ms": round(m["median_ms"], 4),
            "mean_trimmed_ms": round(m["mean_trimmed_ms"], 4),
            "iqr": m["stats"]["iqr"],
            "cv": m["stats"]["cv"],
            "raw_ms": m["raw_ms"],
            "tiers": attempts,
        }
    else:
        time_block = {"median_ms": None, "mean_trimmed_ms": None, "iqr": None,
                      "cv": None, "raw_ms": [], "tiers": attempts}
    mem_block = m.get("mem") or {"peak_tracemalloc_mb": None, "peak_rss_mb": None}
    return {
        "run_id": run_id,
        "task_id": task["task_id"],
        "dataset": task.get("dataset"),
        "method": method,
        "started_at": started,
        "manifest": manifest(model=model, model_version=model_version,
                             temperature=temperature, seed=seed,
                             dataset_path=dataset_path),
        "status": status,
        "error": m.get("error"),
        "time": time_block,
        "mem": mem_block,
        "speedup": None,
        "mem_ratio": None,
        "downgraded": m.get("downgraded", False),
        "protocol_version": PROTOCOL_VERSION,
        "code_source": "canonical" if code is None else "candidate",
    }


def calibrate_task(task, dataset_path=None, model="canonical", seed=42, **kw):
    """给 B 用：返回 difficulty 需要的客观量（A 不改题库，B 自己回填）。"""
    started = datetime.now().isoformat(timespec="seconds")
    m = measure_code(task["code"], task["test"], kind=task.get("kind", "assert"),
                     entry=task.get("entry_point", "solution"), **kw)
    if m.get("status") != "ok":
        return {"task_id": task["task_id"], "status": m.get("status"),
                "error": m.get("error"), "started_at": started}
    return {
        "task_id": task["task_id"],
        "status": "ok",
        "started_at": started,
        "difficulty": {
            "baseline_ms": round(m["mean_trimmed_ms"], 4),  # 与主口径一致，别让 B 的题库用另一套
            "cv": m["stats"]["cv"],
            "raw_ms": m["raw_ms"],
            "reps": m["reps"],
            "mem": m["mem"],
        },
        "manifest": manifest(model=model, seed=seed, dataset_path=dataset_path),
        "protocol_version": PROTOCOL_VERSION,
    }


# ---------------------------------------------------------------------------
# 自检 + CLI
# ---------------------------------------------------------------------------
def selftest():
    print("== trimmed_mean ==")
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    got = trimmed_mean(vals, 2)
    assert abs(got - 6.5) < 1e-9, got
    print("  ok:", got)

    print("== 端到端（synthetic） ==")
    code = "def f(n):\n    return sum(range(n))\n"
    test = "assert f(2000) == 1999000\n"
    r = measure_code(code, test, kind="assert", e_reps=4, target_batch_s=0.05)
    assert r["status"] == "ok", r
    assert r["mem"]["peak_tracemalloc_mb"] is not None
    assert r["mem"]["peak_rss_mb"] is not None
    print("  ok: median=%.4fms cv=%.3f mem(tm)=%s mem(rss)=%s"
          % (r["median_ms"], r["stats"]["cv"],
             r["mem"]["peak_tracemalloc_mb"], r["mem"]["peak_rss_mb"]))

    print("== 错误答案 -> incorrect ==")
    r = measure_code(code, "assert f(2000) == 0\n", kind="assert", e_reps=2, target_batch_s=0.05)
    assert r["status"] == "incorrect", r
    print("  ok:", r["error"])

    print("== 卡死 -> timeout（不是 incorrect） ==")
    slow = "import time\ndef g():\n    time.sleep(1.5)  # 超过看门狗\n"
    r = measure_code(slow, "g()\n", kind="assert", e_reps=2, target_batch_s=2.0,
                     stale_s=0.5, max_reps=5)
    assert r["status"] == "timeout", r
    assert "heartbeat stale" in (r.get("error") or ""), r
    print("  ok:", r.get("error"))

    print("== 回归1：单次 2s 的大任务必须 ok（不能判 timeout） ==")
    big = "import time\ndef h():\n    time.sleep(2.0)\n"
    r = measure_code(big, "h()\n", kind="assert", e_reps=2, target_batch_s=0.25)
    assert r["status"] == "ok", r
    print("  ok: status=ok, median=%.0f ms, reps=%d" % (r["median_ms"], r["reps"]))

    print("== 回归2：计时不得被 tracemalloc 污染（且 memory 档确实更慢） ==")
    alloc_code = "def f(n):\n    return [i * i for i in range(n)]\n"
    alloc_test = "assert f(2000)[-1] == 3996001\n"
    t_run = run_child(_build_child(alloc_code, alloc_test, "assert", 300, DEFAULT_MAX_MEM_MB,
                                   "f", "time"), budget_s=60.0)
    m_run = run_child(_build_child(alloc_code, alloc_test, "assert", 300, DEFAULT_MAX_MEM_MB,
                                   "f", "mem"), budget_s=60.0)
    t_ms = t_run.result["batch_s"] / 300.0 * 1000.0
    m_ms = m_run.result["batch_s"] / 300.0 * 1000.0
    ratio_mem = m_ms / t_ms
    res = measure_code(alloc_code, alloc_test, e_reps=3, target_batch_s=0.05)
    ratio_used = res["median_ms"] / t_ms
    print("  time=%.4fms  mem=%.4fms  mem/time=%.2fx  harness/timed=%.2fx"
          % (t_ms, m_ms, ratio_mem, ratio_used))
    assert ratio_mem > 1.2, "tracemalloc 本该明显更慢，说明这版没插桩？"
    assert 0.7 <= ratio_used <= 1.3, "计时口径被污染：harness/timed=%.2f" % ratio_used
    print("  ok: 计时走的是无插桩路径")

    print("== 回归3：逐用例诊断与整块计时同口径 ==")
    hv = load_tasks("humaneval", ROOT / "datasets" / "sample_humaneval_20.json", ids=["31"])[0]
    mc = measure_cases(hv["code"], hv["test"], kind="humaneval",
                       entry=hv["entry_point"], case_reps=200)
    assert mc["status"] == "ok", mc
    whole = measure_code(hv["code"], hv["test"], kind="humaneval",
                         entry=hv["entry_point"], e_reps=3)
    ratio = mc["total_ms"] / whole["median_ms"]
    print("  逐用例合计=%.4fms  整块=%.4fms  比值=%.2f  最慢=%s"
          % (mc["total_ms"], whole["median_ms"], ratio,
             (mc["slowest"]["src"] or "")[:40]))
    # 亚毫秒题上这条比值本身有 10%–40% 抖动（机器负载、GC 时机）；这里只验证「同一条量级/口径」，
    # 不验证精度——精度由 CV 门槛和 E=12 去极值负责。
    assert 0.7 <= ratio <= 2.0, "逐用例与整块口径不一致: %.2f" % ratio
    assert "13441" in mc["slowest"]["src"], mc["slowest"]

    print("== 回归4：相对 workdir 可用，且脚本不落在 workdir ==")
    cwd0 = os.getcwd()
    try:
        os.chdir(str(ROOT))
        rel = "results/_tmp_relcheck"
        r = measure_code(alloc_code, alloc_test, e_reps=2, target_batch_s=0.05, workdir=rel)
        assert r["status"] == "ok", r
        leftovers = list((ROOT / rel).glob("hb_*.py"))
        assert not leftovers, "临时脚本不该落在 workdir: %s" % leftovers[:3]
    finally:
        os.chdir(cwd0)
    print("  ok: 相对 workdir 正常，workdir 内无残留脚本")

    print("== 回归5：EffiBench 的 test_block 必须把 solution 绑成实例 ==")
    # 背景：EffiBench 测试块写的是 `assert solution.f(1)`（实例），代码里是 `class Solution`。
    # 旧实现只绑 candidate，导致优化线 F2/F3/F4 在 EffiBench 上静默退化成 F1。
    effi_code = "class Solution:\n    def f(self, x):\n        return x + 1\n"
    effi_test = "assert solution.f(1) == 2\nassert solution.f(41) == 42\n"
    mc = measure_cases(effi_code, effi_test, kind="test_block", entry="solution", case_reps=50)
    assert mc["status"] == "ok", mc
    # 注意：test_block 目前不拆用例（split_cases 只处理 humaneval 的逐行 assert），
    # 所以整块算 1 条；优化线若要在 EffiBench 上拿「最慢用例」，需要再补拆分能力。
    assert mc["cases"], mc
    whole5 = measure_code(effi_code, effi_test, kind="test_block", entry="solution",
                         e_reps=2, target_batch_s=0.02)
    assert whole5["status"] == "ok", whole5
    # 也要支持把入口传成类名（Solution）这一半写法
    mc2 = measure_cases(effi_code, effi_test, kind="test_block", entry="Solution", case_reps=50)
    assert mc2["status"] == "ok", mc2
    print("  ok: entry=solution 与 entry=Solution 都能测；用例数=%d（test_block 不拆分）"
          % len(mc["cases"]))

    print("\nALL SELFTESTS PASSED")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="统一测量 harness（E=12 去极值 + 双口径内存 + 心跳超时）")
    ap.add_argument("--dataset", choices=["humaneval", "effibench"], default="humaneval")
    ap.add_argument("--path", default="")
    ap.add_argument("--tasks", default="", help="逗号分隔的题号，如 31,40,96")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--code", choices=["canonical", "none"], default="canonical")
    ap.add_argument("--code-file", default="")
    ap.add_argument("--e-reps", type=int, default=E_REPS)
    ap.add_argument("--tier", choices=["native", "auto", "t2", "t3"], default="native",
                    help="输入阶梯：native 用数据集自带用例；auto 从 native 起，<2ms 自动升 t2/t3")
    ap.add_argument("--min-ms", type=float, default=2.0, help="低于该耗时即视为噪声地板，触发升档")
    ap.add_argument("--target-batch-s", type=float, default=TARGET_BATCH_S)
    ap.add_argument("--out-dir", default="results/golden")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    default_path = ROOT / "datasets" / ("sample_humaneval_20.json" if args.dataset == "humaneval"
                                        else "dataset.json")
    path = Path(args.path) if args.path else default_path
    if not path.exists():
        print("数据集不存在: %s" % path, file=sys.stderr)
        return 2

    ids = [x.strip() for x in args.tasks.split(",")] if args.tasks else None
    tasks = load_tasks(args.dataset, path, ids=ids, limit=args.limit or None)
    if not tasks:
        print("没有匹配的题目（--tasks=%s）" % args.tasks, file=sys.stderr)
        return 2

    code_override = None
    if args.code == "none":
        if not args.code_file:
            print("--code none 需要 --code-file", file=sys.stderr)
            return 2
        code_override = Path(args.code_file).read_text(encoding="utf-8")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for i, task in enumerate(tasks, 1):
        print("[%d/%d] %s" % (i, len(tasks), task["task_id"]))
        tier_plan = None
        if args.tier != "native" and _inputs is not None:
            tier_plan = _inputs.auto_plan(task) if args.tier == "auto" else _inputs.tiers(task, args.tier)
        res = measure_task(task, code=code_override, dataset_path=path, tier_plan=tier_plan,
                           min_ms=args.min_ms, e_reps=args.e_reps,
                           target_batch_s=args.target_batch_s)
        results.append(res)
        if res["status"] == "ok":
            print("    status=ok  median=%.4f ms  cv=%.3f  tm=%.2f MB  rss=%.1f MB"
                  % (res["time"]["median_ms"], res["time"]["cv"],
                     res["mem"]["peak_tracemalloc_mb"] or 0, res["mem"]["peak_rss_mb"] or 0))
        else:
            print("    status=%s  %s" % (res["status"], res.get("error")))

    out = out_dir / ("golden_%s.json" % run_id)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已写入 %s（%d 题，protocol=%s）" % (out, len(results), PROTOCOL_VERSION))
    ok = sum(1 for r in results if r["status"] == "ok")
    print("汇总: ok=%d / %d，timeout=%d，incorrect=%d"
          % (ok, len(results),
             sum(1 for r in results if r["status"] == "timeout"),
             sum(1 for r in results if r["status"] == "incorrect")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())











