"""
主流程：Base 生成 → 正确性/计时 → Vanilla / PerfCodeGen / Ours 三组优化 → 再正确性/计时。
结果落盘 results/results.json，运行日志落盘 logs/，支持断点续跑。
"""
import json
import os
import sys
import time
from datetime import datetime

from selected_tasks import build_tasks
import llm_client
import prompts
import sandbox

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")
LOGS_DIR = os.path.join(HERE, "logs")
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
RESULTS_FILE = os.path.join(RESULTS_DIR, "results_v2.json")
LOG_FILE = os.path.join(LOGS_DIR, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


class Tee:
    """同时写 stdout 和日志文件"""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()
    def flush(self):
        for s in self.streams:
            s.flush()


_log_fh = open(LOG_FILE, "w", encoding="utf-8")
sys.stdout = Tee(sys.__stdout__, _log_fh)
sys.stderr = Tee(sys.__stderr__, _log_fh)
print(f"[log] run started at {datetime.now().isoformat()}, log file: {LOG_FILE}", flush=True)


def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_results(d):
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def ask_llm(prompt: str, retries: int = 2) -> str:
    last = None
    for i in range(retries + 1):
        try:
            return llm_client.chat(prompt, max_tokens=2048)
        except llm_client.FreeQuotaExhausted:
            raise
        except Exception as e:
            last = e
            print(f"   [llm retry {i+1}] {type(e).__name__}: {e}", flush=True)
            time.sleep(2)
    raise last


def run_one_task(task: dict, results: dict):
    tid = task["task_id"]
    if tid in results and results[tid].get("done"):
        print(f"[skip] {tid} already done", flush=True)
        return
    print(f"\n=== {tid} ({task['entry_point']}) ===", flush=True)
    rec = {"task_id": tid, "entry_point": task["entry_point"]}

    # ---- Base：让 LLM 写初始代码 ----
    base_resp = ask_llm(prompts.base_prompt(task["prompt"]))
    rec["base_raw"] = base_resp
    chk = sandbox.check_correctness(base_resp, task["entry_point"], task["test"])
    rec["base_correct"] = chk.get("correct", False)
    rec["base_check_err"] = chk.get("error")

    if not chk.get("correct"):
        print(f"  base WRONG: {chk.get('error')}; trying one correctness repair", flush=True)
        repaired = ask_llm(prompts.correctness_prompt(task["prompt"], base_resp, chk.get("error") or "test failed"))
        rec["repair_raw"] = repaired
        repair_check = sandbox.check_correctness(repaired, task["entry_point"], task["test"])
        rec["repair_correct"] = repair_check.get("correct", False)
        rec["repair_check_err"] = repair_check.get("error")
        if not repair_check.get("correct"):
            rec["done"] = True
            results[tid] = rec
            save_results(results)
            return
        base_resp = repaired
    rec["effective_base_correct"] = True
    rec["effective_base_raw"] = base_resp

    # 计时 + profile（在最大输入上）
    t0 = sandbox.time_code(base_resp, task["entry_point"], task["test"],
                           task["tiers"], do_profile=True)
    rec["base_times"] = t0.get("times", [])
    rec["base_profile"] = t0.get("profile", [])
    rec["base_profile_err"] = t0.get("profile_error")
    measured = [x for x in rec["base_times"] if "median_ms" in x]
    if not measured:
        rec["measurement_error"] = "No input completed timing"
        rec["done"] = True
        results[tid] = rec
        save_results(results)
        return
    base_slowest = max(measured, key=lambda x: x["median_ms"])
    print(f"  base OK, slowest: {base_slowest['desc']} = {base_slowest['median_ms']} ms", flush=True)

    # ---- 3 组优化 ----
    for method, builder in [
        ("vanilla", lambda code: prompts.vanilla_prompt(task["prompt"], code)),
        ("perfcodegen", lambda code: prompts.perfcodegen_prompt(
            task["prompt"], code,
            slowest_desc=base_slowest["desc"], slowest_ms=base_slowest["median_ms"])),
        ("ours", lambda code: prompts.ours_prompt(
            task["prompt"], code, rec["base_times"], rec["base_profile"])),
    ]:
        opt_resp = ask_llm(builder(base_resp))
        rec[f"{method}_raw"] = opt_resp
        chk2 = sandbox.check_correctness(opt_resp, task["entry_point"], task["test"])
        rec[f"{method}_correct"] = chk2.get("correct", False)
        rec[f"{method}_check_err"] = chk2.get("error")
        if chk2.get("correct"):
            t1 = sandbox.time_code(opt_resp, task["entry_point"], task["test"],
                                   task["tiers"], do_profile=False)
            rec[f"{method}_times"] = t1.get("times", [])
            opt_measured = [x for x in rec[f"{method}_times"] if "median_ms" in x]
            if opt_measured:
                opt_slowest = max(opt_measured, key=lambda x: x["median_ms"])
                print(f"  {method}: OK, slowest = {opt_slowest['median_ms']} ms", flush=True)
            else:
                print(f"  {method}: correct, but all timing inputs failed", flush=True)
        else:
            rec[f"{method}_times"] = []
            print(f"  {method}: WRONG - {chk2.get('error')}", flush=True)

    rec["done"] = True
    results[tid] = rec
    save_results(results)


def main():
    tasks = build_tasks()
    results = load_results()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for t in tasks:
        if only and only not in t["task_id"]:
            continue
        try:
            run_one_task(t, results)
        except llm_client.FreeQuotaExhausted as e:
            print(f"[STOP] {e}; completed task results remain saved", flush=True)
            break
        except Exception as e:
            import traceback
            print(f"[FAIL] {t['task_id']}: {e}\n{traceback.format_exc()}", flush=True)
            results[t["task_id"]] = {"task_id": t["task_id"], "done": False,
                                     "fatal_error": f"{type(e).__name__}: {e}"}
            save_results(results)


if __name__ == "__main__":
    main()
