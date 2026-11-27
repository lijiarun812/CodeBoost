"""统一的代码效率测量核心（只依赖标准库；内存采样在装了 psutil 时自动启用）。

设计目标（对应终期汇报第 4、5 部分）：
1. 噪声可控：自动放大重复次数到 target_time，多轮取样取中位数，并给出 MAD；
2. 口径明确：per-case 逐条断言计时，最慢用例可直接作为反馈；
3. 与旧结果可比：同时给 `mem_mbs`（RSS-时间积分，旧口径）与 `mem_mb_avg`
   （= mem_mbs / 执行时间，等效常驻内存，消除“跑得快所以积分小”的耦合）。

对外接口：
    split_test_cases(test)                      -> (cases, mode)
    check(code, test, entry_point)              -> {ok, error, case}
    measure(code, test, entry_point, ...)       -> 统一结果 dict
    calibrate(code, test, entry_point, ...)     -> repeats
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

MARKER = "__CODEBOOST__"
DEFAULT_TARGET_TIME = 0.25
DEFAULT_MAX_REPEATS = 100000
MIN_REPEATS = 5
DEFAULT_RUNS = 3
DEFAULT_MAX_TIME = 20.0

# ---------------------------------------------------------------------------
# 测试用例切分：把 HumanEval 的 `def check(candidate):` 拆成一条条 assert
# ---------------------------------------------------------------------------
_CHECK_DEF = re.compile(r"^(\s*)def\s+check\s*\(")


def split_test_cases(test_code: str):
    """返回 (cases, mode)。

    mode == "split"：每个 case 是一条独立 assert（用于定位最慢用例）；
    mode == "whole"：函数体里有非 assert 语句或切分失败，整块当一条用例。
    """
    test_code = test_code or ""
    lines = test_code.splitlines()
    start = None
    indent = 0
    for i, ln in enumerate(lines):
        m = _CHECK_DEF.match(ln)
        if m:
            start, indent = i, len(m.group(1))
            break
    if start is None:
        body = textwrap.dedent(test_code).strip()
        return ([body], "whole") if body else ([], "whole")

    body_lines = []
    body_indent = None
    for ln in lines[start + 1:]:
        if not ln.strip():
            continue
        cur = len(ln) - len(ln.lstrip())
        if cur <= indent:  # 函数结束
            break
        body_indent = cur if body_indent is None else body_indent
        body_lines.append(ln)
    if not body_lines:
        return [], "whole"

    cases, cur, has_other = [], [], False
    for ln in body_lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        at_top = (len(ln) - len(ln.lstrip())) == body_indent
        if at_top and s.startswith("assert"):
            if cur:
                cases.append(cur)
            cur = [ln]
        elif at_top:
            has_other = True
            cur.append(ln)
        else:
            cur.append(ln)  # assert 的续行
    if cur:
        cases.append(cur)

    if has_other or not cases:
        block = textwrap.dedent("\n".join(body_lines)).strip()
        return ([block], "whole") if block else ([], "whole")
    return [textwrap.dedent("\n".join(c)).strip() for c in cases], "split"


# ---------------------------------------------------------------------------
# 被测程序模板：子进程内 逐 case 计时 + 可选 RSS 采样
# ---------------------------------------------------------------------------
_PROGRAM = '''\
import json, sys, time, traceback

try:  # 内存采样是可选的：没装 psutil 时只测时间
    import psutil
except Exception:  # noqa: BLE001
    psutil = None

__CB_CODE__

__CB_CASES__ = __CB_CASES_LITERAL__


def __cb_env():
    g = {"__name__": "__codeboost_case__"}
    g.setdefault("candidate", globals().get(__CB_ENTRY__))
    return g


def __cb_time(src, reps, g):
    co = compile(src, "<codeboost-case>", "exec")
    t0 = time.perf_counter()
    for _ in range(reps):
        exec(co, g)
    return time.perf_counter() - t0


def __cb_sampler_start(interval=0.002):
    if psutil is None:
        return None
    proc = psutil.Process()

    class Sampler:
        def __init__(self):
            import threading
            self.series = []
            self.stop = threading.Event()
            self.thread = threading.Thread(target=self.run, daemon=True)

        def run(self):
            while not self.stop.is_set():
                self.series.append((time.perf_counter(), proc.memory_info().rss / 1048576.0))
                self.stop.wait(interval)

        def start(self):
            self.thread.start()

        def finish(self):
            self.stop.set()
            self.thread.join(timeout=1.0)
            if not self.series:
                return None, None
            integral, peak = 0.0, 0.0
            for i in range(1, len(self.series)):
                dt = self.series[i][0] - self.series[i - 1][0]
                integral += (self.series[i][1] + self.series[i - 1][1]) / 2.0 * dt
                peak = max(peak, self.series[i][1])
            return integral, peak

    s = Sampler()
    s.start()
    return s


def main():
    mode = __CB_MODE__
    reps = __CB_REPEATS__
    if mode == "check":
        out = []
        for i, src in enumerate(__CB_CASES__):
            g = __cb_env()
            try:
                exec(compile(src, "<codeboost-check-%d>" % i, "exec"), g)
                out.append({"ok": True})
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc().strip().splitlines()
                out.append({
                    "ok": False,
                    "case": i,
                    "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
                    "where": tb[-2].strip() if len(tb) >= 2 else "",
                })
        print("__CODEBOOST__" + json.dumps({"cases": len(__CB_CASES__), "checks": out}))
        return

    sampler = __cb_sampler_start()
    t_start = time.perf_counter()
    g = __cb_env()
    per = [__cb_time(src, reps, g) for src in __CB_CASES__]
    overhead = __cb_time("pass", reps, g)
    wall = time.perf_counter() - t_start
    mem = sampler.finish() if sampler is not None else (None, None)
    print("__CODEBOOST__" + json.dumps({
        "per_case": per,
        "overhead": overhead,
        "wall": wall,
        "mem_mbs": mem[0],
        "peak_mb": mem[1],
    }))


main()
'''


def _build_program(code, cases, entry_point, repeats, mode):
    src = _PROGRAM
    src = src.replace("__CB_CODE__", code)
    src = src.replace("__CB_CASES_LITERAL__", repr(list(cases)))
    src = src.replace("__CB_ENTRY__", repr(entry_point))
    src = src.replace("__CB_MODE__", repr(mode))
    src = src.replace("__CB_REPEATS__", str(int(repeats)))
    return src


def _run(code, cases, entry_point, repeats, mode, max_time, workdir):
    program = _build_program(code, cases, entry_point, repeats, mode)
    wd = Path(workdir) if workdir else Path(tempfile.gettempdir())
    tmp = wd / ("codeboost_%d_%d.py" % (os.getpid(), int(time.time() * 1e6)))
    try:
        tmp.write_text(program, encoding="utf-8")
    except OSError:
        tmp = Path(tempfile.gettempdir()) / tmp.name
        tmp.write_text(program, encoding="utf-8")
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

    out = proc.stdout or ""
    idx = out.rfind(MARKER)
    if idx < 0:
        err = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": (err[-1][:300] if err else "exit %d" % proc.returncode)}
    try:
        payload = json.loads(out[idx + len(MARKER):].strip().splitlines()[0])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "bad marker payload: %s" % e}
    payload["ok"] = True
    return payload


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def check(code, test, entry_point="solution", max_time=DEFAULT_MAX_TIME, workdir=None):
    """逐条用例跑一次，返回 {ok, error, case, n_cases, case_mode}。"""
    cases, mode = split_test_cases(test)
    if not cases:
        return {"ok": False, "error": "no_test_cases", "n_cases": 0, "case_mode": mode}
    res = _run(code, cases, entry_point, 0, "check", max_time, workdir)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "n_cases": len(cases), "case_mode": mode}
    for item in res["checks"]:
        if not item["ok"]:
            return {
                "ok": False,
                "error": item["error"],
                "where": item.get("where", ""),
                "case": item.get("case"),
                "case_src": cases[item.get("case", 0)],
                "n_cases": len(cases),
                "case_mode": mode,
            }
    return {"ok": True, "n_cases": len(cases), "case_mode": mode}


def calibrate(code, test, entry_point="solution", target_time=DEFAULT_TARGET_TIME,
              max_repeats=DEFAULT_MAX_REPEATS, max_time=DEFAULT_MAX_TIME, workdir=None):
    """用 3 次重复做探针，估算单次耗时后放大到大致的 target_time。"""
    cases, _ = split_test_cases(test)
    probe = _run(code, cases, entry_point, 25, "time", max_time, workdir)
    if not probe.get("ok"):
        return None, probe.get("error")
    per_iter = sum(probe["per_case"]) / 25.0
    if per_iter <= 1e-7:
        return max_repeats, None
    reps = int(target_time / per_iter)
    return max(MIN_REPEATS, min(max_repeats, reps)), None


def measure(code, test, entry_point="solution", runs=DEFAULT_RUNS, repeats=None,
            target_time=DEFAULT_TARGET_TIME, max_repeats=DEFAULT_MAX_REPEATS,
            max_time=DEFAULT_MAX_TIME, workdir=None):
    """完整测量：先校验正确性，再校准重复次数，多轮取样取中位数。"""
    cases, case_mode = split_test_cases(test)
    if not cases:
        return {"ok": False, "error": "no_test_cases", "case_mode": case_mode}

    chk = check(code, test, entry_point, max_time, workdir)
    if not chk.get("ok"):
        out = dict(chk)
        out["ok"] = False
        return out

    reps = repeats
    if not reps:
        reps, err = calibrate(code, test, entry_point, target_time, max_repeats, max_time, workdir)
        if reps is None:
            return {"ok": False, "error": "calibrate_failed: %s" % err, "case_mode": case_mode}

    samples = []
    for _ in range(max(1, int(runs))):
        res = _run(code, cases, entry_point, reps, "time", max_time, workdir)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error"), "case_mode": case_mode}
        samples.append(res)
    if not samples:
        return {"ok": False, "error": "no_samples", "case_mode": case_mode}

    # 每个 case 的净耗时 = (实测 - exec 开销) / 重复次数，换算成“单次执行”；
    # 取样估计用 min-of-runs（benchmark 惯例：最小值最接近真实计算时间）
    per_case_runs = []
    for s in samples:
        oh = s["overhead"]
        per_case_runs.append([max(0.0, (dt - oh) / float(reps)) for dt in s["per_case"]])
    n = len(cases)
    per_case_ms = []
    for i in range(n):
        vals = [r[i] for r in per_case_runs]
        per_case_ms.append(round(min(vals) * 1000.0, 4))
    totals = [sum(r) * 1000.0 for r in per_case_runs]
    total_ms = round(min(totals), 4)
    med = statistics.median(totals)
    mad_ms = round(statistics.median([abs(t - med) for t in totals]), 4) if len(totals) > 1 else 0.0
    slow = max(range(n), key=lambda i: per_case_ms[i]) if n else 0

    mems = [s.get("mem_mbs") / float(reps) for s in samples if s.get("mem_mbs") is not None]
    peaks = [s.get("peak_mb") for s in samples if s.get("peak_mb") is not None]
    mem_mbs = round(statistics.median(mems), 4) if mems else None
    peak_mb = round(max(peaks), 3) if peaks else None
    total_s = total_ms / 1000.0
    mem_mb_avg = round(mem_mbs / total_s, 3) if (mem_mbs is not None and total_s > 0) else None

    return {
        "ok": True,
        "case_mode": case_mode,
        "n_cases": n,
        "repeats": reps,
        "runs": len(samples),
        "total_ms": total_ms,
        "runs_ms": [round(t, 4) for t in totals],
        "mad_ms": mad_ms,
        "rel_mad": round(mad_ms / total_ms, 4) if total_ms else 0.0,
        "per_case_ms": per_case_ms,
        "slowest": {"idx": slow, "ms": per_case_ms[slow] if n else 0.0,
                    "src": cases[slow] if n else ""},
        "mem_mbs": mem_mbs,
        "mem_mb_avg": mem_mb_avg,
        "peak_mb": peak_mb,
    }


def measure_file(path):
    """读取单题结果 JSON（便于离线重测脚本复用）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))

