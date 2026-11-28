"""Resumable G/F replay from sealed prompts; model calls require explicit opt-in."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "results" / "exploratory" / "gf5_2026-11-28"
MANIFEST = ROOT / "frozen5_shared_manifest_2026-11-28.json"
PREFLIGHT = ROOT / "frozen5_prompt_preflight_2026-11-28.json"
ORIGINAL_REPORT = ROOT / "frozen5_gf_paired_trial_2026-11-28.json"
OUTPUT: Path | None = None
MANIFEST_SHA = "82419f2f5867b373e6fe44ad3639adc1b5e022ba9f3ed081be9a3a1c58768f87"
PREFLIGHT_SHA = "54a052065b8687bf23470d7a60dc0e8a579cce73690c260a3f07fc25b6b953fe"
ARMS = ("G0", "G1", "G2", "G3", "F0", "F1", "F2", "F3", "F4")
sys.path.insert(0, str(REPO))

from eval import harness
from eval.gate import check as check_gate
from eval.space import pareto_accept
from optimize.prompts import render_prompt


def save(doc: dict) -> None:
    if OUTPUT is None:
        raise RuntimeError("replay_output_not_selected")
    doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(OUTPUT)


def extract_code(content: str) -> tuple[str | None, str | None]:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", content, flags=re.I | re.S)
    for text in blocks + [content]:
        code = text.strip()
        try:
            ast.parse(code)
        except SyntaxError:
            continue
        return code, None
    return None, "no_parseable_python_program"


def task_and_tier(entry: dict) -> tuple[dict, dict]:
    frozen = {key: entry[key] for key in ("tier", "n", "gen", "args_hash", "test")}
    task = {"task_id": entry["task_id"], "dataset": entry["dataset"],
            "kind": entry["kind"], "entry_point": entry["entry_point"],
            "description": entry["description"], "code": entry["initial_code"],
            "test": entry["test"], "inputs": [frozen]}
    return task, frozen


def paired(baseline: dict, candidate: dict, entry: dict) -> bool:
    def key(value: dict):
        tiers = (value.get("time") or {}).get("tiers") or []
        final = tiers[-1] if tiers else {}
        return (value.get("task_id"), final.get("tier"), final.get("args_hash"))
    required = (entry["task_id"], entry["tier"], entry["args_hash"])
    return key(baseline) == key(candidate) == required


def finish_measurement(row: dict, entry: dict, pre: dict) -> None:
    code = row.get("candidate_code")
    if code is None:
        row.update({"state": "complete", "candidate_status": "not_measured",
                    "measurement_error": row.get("parse_error") or "no_candidate_code"})
        return
    task, frozen = task_and_tier(entry)
    try:
        result = harness.measure_task(
            task, code=code, method="diagnostic", tier_plan=[frozen],
            model=row["returned_model_id"], model_version=row["returned_model_id"],
            temperature=0.7, seed=42,
        )
    except Exception as exc:
        row.update({"state": "complete", "candidate_status": "measurement_exception",
                    "measurement_error": type(exc).__name__})
        return
    row["candidate_result"] = result
    row["candidate_status"] = result.get("status")
    row["paired_task_tier_hash"] = paired(pre["baseline"], result, entry)
    if not row["paired_task_tier_hash"]:
        row["measurement_error"] = "task_tier_args_hash_mismatch"
    elif result.get("status") == "ok":
        try:
            row["joint_diagnostic_decision"] = pareto_accept(pre["baseline"], result,
                                                               rss_baseline_mb=None)
        except Exception as exc:
            row["joint_diagnostic_decision_error"] = type(exc).__name__
    if row["arm"] == "F4":
        diagnostics = pre["arms"]["F4"]["diagnostic"]
        h3 = hashlib.sha256(pre["arms"]["F3"]["prompt"].encode()).hexdigest()
        h4 = hashlib.sha256(pre["arms"]["F4"]["prompt"].encode()).hexdigest()
        row["f3_prompt_sha256"] = h3
        row["f4_prompt_sha256"] = h4
        row["f4_gate"] = check_gate(
            diagnostics=diagnostics, feedback=pre["arms"]["F4"]["feedback"],
            result_status=result.get("status"), harness_status=result.get("status"),
            prompt_hash_f3=h3, prompt_hash_f4=h4, formal_task=False, method="ours",
        )
        row["formal_block_reason"] = "G6_non_formal_task"
    row["state"] = "complete"


def main() -> None:
    global OUTPUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-model", action="store_true",
                        help="Explicitly enable paid model calls; default is read-only preflight")
    parser.add_argument("--output", type=Path,
                        help="New replay journal path; never use the archived original report")
    parser.add_argument("--first-only", action="store_true")
    args = parser.parse_args()
    from experiments.frozen_pairs.preflight import check_batch
    check_batch("gf5")
    if not args.execute_model:
        print("gf5 preflight passed; no model call made")
        return
    if args.output is None:
        parser.error("--execute-model requires --output")
    OUTPUT = args.output.resolve()
    if OUTPUT == ORIGINAL_REPORT.resolve():
        parser.error("archived original report is immutable; choose a new --output")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    from experiment import llm_client
    manifest_bytes, preflight_bytes = MANIFEST.read_bytes(), PREFLIGHT.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA:
        raise RuntimeError("shared_manifest_changed")
    if hashlib.sha256(preflight_bytes).hexdigest() != PREFLIGHT_SHA:
        raise RuntimeError("prompt_preflight_changed")
    manifest = json.loads(manifest_bytes)
    preflight = json.loads(preflight_bytes)
    entries = {r["task_id"]: r for r in manifest["entries"]}
    pres = {r["task_id"]: r for r in preflight["rows"]}
    if set(entries) != set(pres) or len(entries) != 5:
        raise RuntimeError("preflight_manifest_task_mismatch")
    import os
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    base_url, _, model = llm_client.get_client()  # credential never persisted or printed
    if (provider, model, base_url) != (
        "dashscope", "qwen4-max", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ):
        raise RuntimeError("provider_model_endpoint_mismatch")
    if OUTPUT.exists():
        doc = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if (doc.get("shared_manifest_sha256") != MANIFEST_SHA or
                doc.get("prompt_preflight_sha256") != PREFLIGHT_SHA):
            raise RuntimeError("existing_trial_belongs_to_other_inputs")
    else:
        doc = {
            "classification": "冻结题上的小规模探索性配对试跑",
            "formal_task": False, "formal_result_writes_enabled": False,
            "formal_result_written": False, "historical_g_results_included": False,
            "shared_manifest_sha256": MANIFEST_SHA,
            "prompt_preflight_sha256": PREFLIGHT_SHA,
            "source_file_sha256": manifest["source_file_sha256"],
            "provider": provider, "requested_model_id": model, "base_url": base_url,
            "generation_parameters": {"temperature": 0.7, "enable_thinking": False,
                                      "max_tokens": 2048, "seed_sent": False},
            "max_generation_calls": 45, "generation_calls_attempted": 0,
            "cost_usd": None, "immutable_model_revision": None,
            "rows": [], "stop_reason": None,
            "notes": ["No paid request is automatically retried, including an interrupted in-flight call.",
                      "One candidate per task and arm, all sharing the sealed five-task manifest.",
                      "Historical G prompts used a different common frame and worker; see prompt preflight differences.",
                      "Harness seed=42 is measurement metadata; model client does not send a generation seed.",
                      "line_profiler and G3 profiled line times are diagnostic only, never final time metrics."],
        }
        save(doc)
    for entry in manifest["entries"]:
        pre = pres[entry["task_id"]]
        if pre["baseline"].get("status") != "ok":
            raise RuntimeError("selected_baseline_not_ok")
        for arm in ARMS:
            key = (entry["task_id"], arm)
            matching = [r for r in doc["rows"] if (r["task_id"], r["arm"]) == key]
            if len(matching) > 1:
                raise RuntimeError("duplicate_arm_record")
            if matching:
                row = matching[0]
                if row["state"] == "complete":
                    continue
                if row["state"] == "in_flight":
                    doc["stop_reason"] = "unresolved_paid_call_do_not_retry"
                    save(doc)
                    return
                if row["state"] == "response_received":
                    finish_measurement(row, entry, pre)
                    save(doc)
                    continue
                raise RuntimeError("unknown_resume_state")
            if doc["generation_calls_attempted"] >= 45:
                doc["stop_reason"] = "call_cap"
                save(doc)
                return
            feedback = pre["arms"][arm]
            if feedback["prompt"] != render_prompt(entry["description"], entry["initial_code"], feedback["feedback"]):
                raise RuntimeError("shared_prompt_frame_mismatch")
            row = {"task_id": entry["task_id"], "arm": arm,
                   "tier": entry["tier"], "args_hash": entry["args_hash"],
                   "test_sha256": entry["test_sha256"],
                   "initial_code_sha256": entry["initial_code_sha256"],
                   "description_sha256": entry["description_sha256"],
                   "requested_arm": arm, "effective_arm": feedback["effective_arm"],
                   "degrade_reason": feedback["degrade_reason"],
                   "feedback": feedback["feedback"], "prompt": feedback["prompt"],
                   "prompt_sha256": hashlib.sha256(feedback["prompt"].encode()).hexdigest(),
                   "diagnostic": feedback["diagnostic"],
                   "state": "in_flight", "paid_call_started_at": datetime.now().isoformat(timespec="seconds"),
                   "returned_model_id": None, "immutable_model_revision": None,
                   "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                   "cost_usd": None, "candidate_code": None, "candidate_status": None,
                   "formal_result_written": False}
            doc["rows"].append(row)
            doc["generation_calls_attempted"] += 1
            save(doc)  # durable intent before sending the paid request
            try:
                meta = llm_client.chat_with_meta(feedback["prompt"], temperature=0.7,
                                                 max_tokens=2048)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                row["state"] = "complete"
                row["api_error_category"] = type(exc).__name__ + (f":HTTP_{status}" if status else "")
                doc["stop_reason"] = "model_call_failed:" + row["api_error_category"]
                save(doc)
                return
            row.update({"state": "response_received", "returned_model_id": meta.get("model"),
                        "raw_model_content": meta.get("content"),
                        "prompt_tokens": meta.get("prompt_tokens"),
                        "completion_tokens": meta.get("completion_tokens"),
                        "total_tokens": meta.get("total_tokens"),
                        "api_latency_s": meta.get("latency_s")})
            save(doc)  # response survives a measurement interruption
            if row["returned_model_id"] != "qwen4-max":
                row["state"] = "complete"
                row["api_error_category"] = "returned_model_id_mismatch"
                doc["stop_reason"] = "returned_model_id_mismatch"
                save(doc)
                return
            row["candidate_code"], row["parse_error"] = extract_code(meta["content"])
            save(doc)
            finish_measurement(row, entry, pre)
            save(doc)
            print(json.dumps({"task_id": entry["task_id"], "arm": arm,
                              "status": row["candidate_status"],
                              "calls": doc["generation_calls_attempted"]}, ensure_ascii=False), flush=True)
            if args.first_only:
                doc["stop_reason"] = "first_call_checkpoint"
                save(doc)
                return
    doc["stop_reason"] = "completed"
    save(doc)


if __name__ == "__main__":
    main()
