"""Resumable M0/M1 replay from sealed prompts; model calls require opt-in."""
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
OUT = REPO / "results" / "exploratory" / "m0m1_2026-11-28"
MANIFEST = OUT / "m0m1_frozen8_manifest_2026-11-28.json"
PREFLIGHT = OUT / "m0m1_frozen8_preflight_2026-11-28.json"
ORIGINAL_REPORT = OUT / "m0m1_frozen8_trial_2026-11-28.json"
REPORT: Path | None = None
MANIFEST_SHA = "520368613083cfc8e2e8fbd788c020a402ccc85c8f294abe6dba50451d465470"
PREFLIGHT_SHA = "924218b50672409443e323c948b75845d059cc389a3951b938aa97c109289aab"
sys.path.insert(0, str(REPO))

from eval import harness
from eval.space import pareto_accept
from optimize.prompts import render_prompt


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(doc: dict) -> None:
    if REPORT is None:
        raise RuntimeError("replay_output_not_selected")
    doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(REPORT)


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


def paired(base: dict, candidate: dict, entry: dict) -> bool:
    def key(r: dict):
        ts = (r.get("time") or {}).get("tiers") or []
        final = ts[-1] if ts else {}
        return r.get("task_id"), final.get("tier"), final.get("args_hash")
    required = entry["task_id"], entry["tier"], entry["args_hash"]
    return key(base) == key(candidate) == required


def finish_measurement(row: dict, entry: dict, baseline: dict) -> None:
    code = row.get("candidate_code")
    if code is None:
        row.update(state="complete", candidate_status="not_measured",
                   measurement_error=row.get("parse_error") or "no_candidate_code",
                   pareto_decision=None)
        return
    frozen = {k: entry[k] for k in ("tier", "n", "gen", "args_hash", "test")}
    task = {"task_id": entry["task_id"], "dataset": entry["dataset"],
            "kind": entry["kind"], "entry_point": entry["entry_point"],
            "description": entry["description"], "code": entry["initial_code"],
            "test": entry["test"], "inputs": [frozen]}
    try:
        result = harness.measure_task(task, code=code, method="diagnostic",
                                      tier_plan=[frozen], model=row["returned_model_id"],
                                      model_version=row["returned_model_id"],
                                      temperature=0.7, seed=42)
        row["candidate_result"] = result
        row["candidate_status"] = result.get("status")
        row["paired_task_tier_hash"] = paired(baseline, result, entry)
        if row["paired_task_tier_hash"]:
            row["pareto_decision"] = pareto_accept(baseline, result, rss_baseline_mb=None)
        else:
            row["measurement_error"] = "task_tier_args_hash_mismatch"
            row["pareto_decision"] = None
    except Exception as exc:
        row.update(candidate_status="measurement_exception",
                   measurement_error=type(exc).__name__, pareto_decision=None)
    row["state"] = "complete"


def main() -> None:
    global REPORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-model", action="store_true",
                        help="Explicitly enable paid model calls; default is read-only preflight")
    parser.add_argument("--output", type=Path,
                        help="New replay journal path; never use the archived original report")
    args = parser.parse_args()
    from experiments.frozen_pairs.preflight import check_batch
    check_batch("m0m1")
    if not args.execute_model:
        print("m0m1 preflight passed; no model call made")
        return
    if args.output is None:
        parser.error("--execute-model requires --output")
    REPORT = args.output.resolve()
    if REPORT == ORIGINAL_REPORT.resolve():
        parser.error("archived original report is immutable; choose a new --output")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    from experiment import llm_client
    mb, pb = MANIFEST.read_bytes(), PREFLIGHT.read_bytes()
    if sha(mb) != MANIFEST_SHA or sha(pb) != PREFLIGHT_SHA:
        raise RuntimeError("sealed_manifest_or_prompt_changed")
    manifest, preflight = json.loads(mb), json.loads(pb)
    if preflight["shared_manifest_sha256"] != MANIFEST_SHA:
        raise RuntimeError("preflight_manifest_mismatch")
    if len(manifest["entries"]) != 8 or len(preflight["rows"]) != 8:
        raise RuntimeError("not_eight_tasks")
    pres = {r["task_id"]: r for r in preflight["rows"]}
    if set(pres) != set(manifest["selected_ids"]):
        raise RuntimeError("task_selection_mismatch")
    import os
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    endpoint, _, model = llm_client.get_client()  # never print or persist the credential
    if (provider, model, endpoint) != (
        "dashscope", "qwen4-max", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ):
        raise RuntimeError("provider_model_endpoint_mismatch")
    if REPORT.exists():
        doc = json.loads(REPORT.read_text(encoding="utf-8"))
        if (doc.get("shared_manifest_sha256"), doc.get("prompt_preflight_sha256")) != (MANIFEST_SHA, PREFLIGHT_SHA):
            raise RuntimeError("existing_report_for_different_trial")
    else:
        doc = {"classification": "冻结题上的 M0/M1 小规模探索性配对试跑",
               "formal_task": False, "formal_result_writes_enabled": False,
               "formal_result_written": False, "historical_g_results_included": False,
               "shared_manifest_sha256": MANIFEST_SHA,
               "prompt_preflight_sha256": PREFLIGHT_SHA,
               "source_file_sha256": manifest["source_file_sha256"],
               "requested_provider": provider, "requested_model_id": model,
               "endpoint": endpoint, "prompt_version": preflight["prompt_version"],
               "generation_parameters": preflight["generation"],
               "acceptance": preflight["acceptance"],
               "primary_metric": preflight["primary_metric"],
               "max_generation_calls": 32, "generation_calls_attempted": 0,
               "cost_usd": None, "immutable_model_revision": None,
               "rows": [], "stop_reason": None,
               "notes": ["M1 combines a program-level memory number with a space objective; effects are not isolated.",
                         "No paid call is automatically retried, including interrupted in-flight requests.",
                         "RSS baseline unavailable in this environment: RSS gate skipped, RSS is not a reward axis.",
                         "Frozen t2 test and hash are unchanged; no diagnostic tier promotion enters final metrics.",
                         "Harness seed=42 is measurement metadata; no model seed is sent."]}
        save(doc)
    for entry in manifest["entries"]:
        pre = pres[entry["task_id"]]
        base = pre["baseline"]
        if base.get("status") != "ok" or not paired(base, base, entry):
            raise RuntimeError("invalid_baseline_pairing")
        for replicate in (1, 2):
            for arm in ("M0", "M1"):
                key = entry["task_id"], arm, replicate
                matches = [r for r in doc["rows"] if (r["task_id"], r["arm"], r["replicate"]) == key]
                if len(matches) > 1:
                    raise RuntimeError("duplicate_call_record")
                if matches:
                    row = matches[0]
                    if row["state"] == "complete":
                        continue
                    if row["state"] == "in_flight":
                        doc["stop_reason"] = "unresolved_paid_call_do_not_retry"
                        save(doc)
                        return
                    if row["state"] == "response_received":
                        finish_measurement(row, entry, base)
                        save(doc)
                        continue
                    raise RuntimeError("unknown_resume_state")
                if doc["generation_calls_attempted"] >= 32:
                    doc["stop_reason"] = "call_cap"
                    save(doc)
                    return
                feedback, prompt = pre[arm]["feedback"], pre[arm]["prompt"]
                if prompt != render_prompt(entry["description"], entry["initial_code"], feedback):
                    raise RuntimeError("prompt_frame_mismatch")
                row = {"task_id": entry["task_id"], "arm": arm, "replicate": replicate,
                       "tier": entry["tier"], "args_hash": entry["args_hash"],
                       "test_sha256": entry["test_sha256"],
                       "initial_code_sha256": entry["initial_code_sha256"],
                       "description_sha256": entry["description_sha256"],
                       "feedback": feedback, "prompt": prompt,
                       "prompt_sha256": sha(prompt.encode()),
                       "requested_arm": arm, "effective_arm": arm,
                       "degrade_reason": None,
                       "state": "in_flight", "paid_call_started_at": datetime.now().isoformat(timespec="seconds"),
                       "returned_model_id": None, "raw_model_content": None,
                       "prompt_tokens": None, "completion_tokens": None,
                       "total_tokens": None, "cost_usd": None,
                       "candidate_code": None, "candidate_status": None,
                       "formal_result_written": False}
                doc["rows"].append(row)
                doc["generation_calls_attempted"] += 1
                save(doc)  # durable intent before a billable request
                try:
                    meta = llm_client.chat_with_meta(prompt, temperature=0.7, max_tokens=2048)
                except Exception as exc:
                    http_status = getattr(getattr(exc, "response", None), "status_code", None)
                    row["state"] = "complete"
                    row["api_error_category"] = type(exc).__name__ + (f":HTTP_{http_status}" if http_status else "")
                    doc["stop_reason"] = "model_call_failed:" + row["api_error_category"]
                    save(doc)
                    return
                row.update(state="response_received", returned_model_id=meta.get("model"),
                           raw_model_content=meta.get("content"),
                           prompt_tokens=meta.get("prompt_tokens"),
                           completion_tokens=meta.get("completion_tokens"),
                           total_tokens=meta.get("total_tokens"),
                           api_latency_s=meta.get("latency_s"))
                save(doc)  # response survives measurement interruption
                if row["returned_model_id"] != "qwen4-max":
                    row["state"] = "complete"
                    row["api_error_category"] = "returned_model_id_mismatch"
                    doc["stop_reason"] = "returned_model_id_mismatch"
                    save(doc)
                    return
                row["candidate_code"], row["parse_error"] = extract_code(meta["content"])
                save(doc)
                finish_measurement(row, entry, base)
                save(doc)
                print(json.dumps({"task_id": entry["task_id"], "arm": arm,
                                  "replicate": replicate, "status": row["candidate_status"],
                                  "decision": (row.get("pareto_decision") or {}).get("decision"),
                                  "calls": doc["generation_calls_attempted"]}, ensure_ascii=False), flush=True)
    doc["stop_reason"] = "completed"
    save(doc)


if __name__ == "__main__":
    main()
