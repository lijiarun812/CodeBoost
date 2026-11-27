#!/usr/bin/env python3
"""热点定位：把「哪一行被执行得最多」变成可喂给模型的证据。

为什么不用 line_profiler：它不是标准库，当前环境也没有安装；而课程场景里
「循环体被执行了多少次」已经足够让模型定位瓶颈（EffiLearner 的行级剖析本质
也是在告诉模型“这行很贵”）。所以这里用 `sys.settrace` 统计**行执行次数**，
零依赖、跨平台、可隔离在子进程里跑并带超时。

输出：
    {"ok": True, "mode": "line_hits", "items": [{line, func, hits, share, src}],
     "total_line_events": N, "elapsed_s": x}
失败时 {"ok": False, "mode": "none", "reason": "..."}，调用方降级为“只给最慢用例”。

用法：
    from eval.hotlines import hotspots, render
    hot = hotspots(code, case_src, "solution")
    print(render(hot))
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = "__CODEBOOST__"

_PROGRAM = r'''
import collections, json, sys

CODE = __CODE__
CASE = __CASE__
ENTRY = __ENTRY__
PRELUDE = __PRELUDE__

# 前导代码（`from typing import *` 这类）单独编译成自己的运行单元，
# 这样用户代码仍然从第 1 行开始，行号不会因为拼接而错位。
_g = {"__name__": "__codeboost_hot__"}
if PRELUDE:
    _pre = {"__name__": "__codeboost_prelude__"}
    exec(compile(PRELUDE, "<prelude>", "exec"), _pre)
    _g.update(_pre)
exec(compile(CODE, "<user_code>", "exec"), _g)
# EffiBench 的测试块引用**实例** `solution`，而代码里是 `class Solution`；
# 这里按 entry 解析出可调用对象并实例化，三类名字都绑上（缺哪个补哪个）。
_entry = _g.get(ENTRY)
if _entry is None and isinstance(_g.get(ENTRY[:1].upper() + ENTRY[1:]), type):
    _entry = _g.get(ENTRY[:1].upper() + ENTRY[1:])
if _entry is None:
    _entry = _g.get("Solution")
if isinstance(_entry, type):
    _entry = _entry()
if _entry is not None:
    _g.setdefault(ENTRY, _entry)
    _g.setdefault("candidate", _entry)
    _g.setdefault("solution", _entry)

_src_lines = CODE.splitlines()
_hits = collections.Counter()


def _tracer(frame, event, arg):
    if event == "line" and frame.f_code.co_filename == "<user_code>":
        _hits[(frame.f_code.co_name, frame.f_lineno)] += 1
    return _tracer


err = None
sys.settrace(_tracer)
try:
    exec(compile(CASE, "<case>", "exec"), _g)
except Exception as e:  # noqa: BLE001
    err = "%s: %s" % (type(e).__name__, str(e)[:200])
finally:
    sys.settrace(None)

items = []
for (fn, ln), n in _hits.most_common(20):
    src = _src_lines[ln - 1] if 0 < ln <= len(_src_lines) else ""
    items.append({"line": ln, "func": fn, "hits": n, "src": src})

print("__CODEBOOST__" + json.dumps({
    "error": err,
    "items": items,
    "total_line_events": sum(_hits.values()),
}))
'''


def _run(code, case_src, entry_point, max_time, workdir, prelude=""):
    program = (_PROGRAM
               .replace("__CODE__", repr(code))
               .replace("__CASE__", repr(case_src))
               .replace("__ENTRY__", repr(entry_point))
               .replace("__PRELUDE__", repr(prelude or "")))
    wd = Path(workdir) if workdir else Path(tempfile.gettempdir())
    path = wd / ("hot_%d_%d.py" % (os.getpid(), int(time.time() * 1e6)))
    try:
        path.write_text(program, encoding="utf-8")
    except OSError:
        path = Path(tempfile.gettempdir()) / path.name
        path.write_text(program, encoding="utf-8")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                              timeout=max_time, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "mode": "none", "reason": "timeout(%ss)" % max_time}
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    out = proc.stdout or ""
    idx = out.rfind(MARKER)
    if idx < 0:
        err = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "mode": "none",
                "reason": (err[-1][:200] if err else "exit %d" % proc.returncode)}
    try:
        payload = json.loads(out[idx + len(MARKER):].strip().splitlines()[0])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mode": "none", "reason": "bad payload: %s" % e}
    payload["elapsed_s"] = round(time.perf_counter() - t0, 3)
    payload["mode"] = "line_hits"
    payload["ok"] = True
    return payload


def hotspots(code, case_src, entry_point="solution", top=3,
             max_time=10.0, workdir=None, prelude=""):
    """返回执行次数最多的 top 行（附带源码文本），用于拼证据包。

    `prelude`：与 harness 相同的前导代码（EffiBench 用 `harness.PREAMBLE`），
    让「诊断时的执行环境」和「被测时的执行环境」一致，否则行号可能对应一次没跑通的执行。
    """
    res = _run(code, case_src, entry_point, max_time, workdir, prelude=prelude)
    if not res.get("ok"):
        return res
    total = res.get("total_line_events") or 0
    items = []
    for item in (res.get("items") or [])[:max(1, int(top))]:
        item = dict(item)
        item["share"] = round(item["hits"] / total, 3) if total else 0.0
        item["src"] = (item.get("src") or "").strip()[:120]
        items.append(item)
    res["items"] = items
    res["top"] = top
    return res


def render(hot, limit=3):
    """渲染成可直接进提示词的英文文本（与论文提示词语言保持一致）。"""
    if not hot or not hot.get("ok"):
        return "(hot-line profiling unavailable: %s)" % ((hot or {}).get("reason") or "unknown")
    lines = ["Most-executed lines inside the slowest test case (line / hits / code):"]
    for it in (hot.get("items") or [])[:limit]:
        lines.append("  L%d  x%d  %s" % (it["line"], it["hits"], it["src"]))
    if hot.get("total_line_events"):
        lines.append("(%d line execution events recorded in total)" % hot["total_line_events"])
    return "\n".join(lines)
