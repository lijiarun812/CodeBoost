"""
沙箱：在独立子进程里跑 LLM 生成的代码，做正确性 + 计时 + line_profiler 热点分析。
父进程通过 stdin 喂 JSON，子进程 stdout 返回 JSON。
"""
import json
import re
import subprocess
import sys
import time
from statistics import median

CHILD = r'''
import json, sys, time, traceit
'''

# 实际子进程脚本（会被 subprocess 调用）
CHILD_SCRIPT = r'''
import json, sys, time, traceback
from statistics import median

def extract_code_block(text):
    # 抽 ```python ... ``` 块；没有就整段返回
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return m.group(1) if m else text

def run():
    req = json.loads(sys.stdin.read())
    code = req["code"]
    entry = req["entry_point"]
    test = req["test"]
    tiers = req.get("tiers", [])
    mode = req.get("mode", "correctness")  # correctness | time | profile

    # 从 LLM 返回的 markdown 代码块里抽纯 Python
    m = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.S)
    if m:
        code = m.group(1)

    # 拼模块代码：用户代码 + HumanEval test
    src = code + "\n\n\n" + test + "\n"

    ns = {}
    try:
        exec(compile(src, "<llm_gen>", "exec"), ns)
    except Exception as e:
        return {"ok": False, "error": f"import/compile failed: {type(e).__name__}: {e}"}

    candidate = ns.get(entry)
    if candidate is None:
        return {"ok": False, "error": f"entry point {entry} not found in generated code"}

    result = {"ok": True}

    if mode in ("correctness", "time", "profile"):
        # 跑 HumanEval 自带 check(candidate)
        try:
            ns["check"](candidate)
            result["correct"] = True
        except AssertionError as e:
            result["correct"] = False
            result["error"] = f"check() failed: {e}"
            return result
        except Exception as e:
            result["correct"] = False
            result["error"] = f"check() raised {type(e).__name__}: {e}"
            return result

    if mode in ("time", "profile"):
        times = []
        for tier in tiers:
            args = tuple(tier["args"])
            # warmup
            try:
                candidate(*args)
            except Exception as e:
                times.append({"desc": tier["desc"], "error": f"{type(e).__name__}: {e}"})
                continue
            runs = []
            for _ in range(3):
                t0 = time.perf_counter()
                candidate(*args)
                runs.append((time.perf_counter() - t0) * 1000)  # ms
            times.append({"desc": tier["desc"], "median_ms": round(median(runs), 3),
                          "runs_ms": [round(x, 3) for x in runs]})
        result["times"] = times

    if mode == "profile":
        # line_profiler 热点分析（在最大那一档输入上）
        try:
            from line_profiler import LineProfiler
            lp = LineProfiler()
            lp.add_function(candidate)
            big_args = tuple(tiers[-1]["args"])
            lp.runcall(candidate, *big_args)
            st = lp.get_stats()
            unit = st.unit
            lines = []
            # 新版 line_profiler: key=(fname, lineno, func_name), value=[(lineno, nhits, total_time), ...]
            for (fname, _top_lineno, func_name), per_lines in st.timings.items():
                for (ln, nhits, total_time) in per_lines:
                    source_lines = code.splitlines()
                    lines.append({
                        "lineno": ln,
                        "func": func_name,
                        "hits": nhits,
                        "total_ms": round(total_time * unit * 1000, 3),
                        "source": source_lines[ln - 1].strip() if 1 <= ln <= len(source_lines) else "",
                    })
            lines.sort(key=lambda x: -x["total_ms"])
            result["profile"] = lines[:10]
        except Exception as e:
            result["profile_error"] = f"{type(e).__name__}: {e}"

    return result


if __name__ == "__main__":
    # 上面 import re 在 run() 里用了，这里补一下
    import re
    try:
        out = run()
    except Exception as e:
        out = {"ok": False, "error": f"child crash: {type(e).__name__}: {e}\n{traceback.format_exc()}"}
    sys.stdout.write(json.dumps(out))
'''


def _run_child(payload: dict, timeout: int = 60) -> dict:
    try:
        p = subprocess.run(
            [sys.executable, "-c", CHILD_SCRIPT],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=timeout, encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True, "error": f"timeout after {timeout}s"}
    if p.returncode != 0:
        return {"ok": False, "error": f"child exit {p.returncode}: {p.stderr[-500:]}"}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"ok": False, "error": f"bad child stdout: {p.stdout[:300]} stderr: {p.stderr[:300]}"}


def check_correctness(code: str, entry_point: str, test: str, timeout: int = 30) -> dict:
    return _run_child({
        "code": code, "entry_point": entry_point, "test": test,
        "mode": "correctness",
    }, timeout=timeout)


def time_code(code: str, entry_point: str, test: str, tiers: list,
              timeout: int = 20, do_profile: bool = False) -> dict:
    """Measure each input separately so one slow input cannot discard the others."""
    result = {"ok": True, "times": []}
    for tier in tiers:
        measured = _run_child({
            "code": code, "entry_point": entry_point, "test": test,
            "tiers": [tier], "mode": "time",
        }, timeout=timeout)
        if not measured.get("ok") or not measured.get("correct", False):
            result["times"].append({"desc": tier["desc"], "error": measured.get("error", "measurement failed"),
                                    "timeout": measured.get("timeout", False)})
        else:
            result["times"].extend(measured.get("times", []))
    if do_profile and tiers:
        profiled = _run_child({
            "code": code, "entry_point": entry_point, "test": test,
            "tiers": [tiers[-1]], "mode": "profile",
        }, timeout=timeout)
        result["profile"] = profiled.get("profile", [])
        if not profiled.get("profile"):
            result["profile_error"] = profiled.get("profile_error", profiled.get("error", "no profile data"))
    return result
