"""Injectable offline Ours runner; no model client or file path is built in.

Formal Result writes are disabled by default. The batch entry checks A's RSS
baseline through eval.rss_baseline.ensure(); generation, measurement, and
writers remain injectable. F4 stays diagnostic-only unless B's frozen bank,
C completeness, and A's executable gate all pass. Writes remain disabled by
default.
"""

from __future__ import annotations

import math
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from eval.gate import check as check_f4_gate
from eval.harness import PROTOCOL_VERSION, measure_task
from eval.rss_baseline import ensure as ensure_rss_baseline
from optimize.acceptance import AcceptanceConfig, Mode
from optimize.feedback import build_feedback
from optimize.frozen_task import verify_frozen_task
from optimize.model_adapter import CandidateGenerator
from optimize.ours_pipeline import _pair_reason, run_offline_loop


def _rss_baseline(ensure_fn: Callable[[], dict], environment_id: str) -> tuple[dict, float | None]:
    """Only A's current valid baseline may enter feedback or the RSS gate."""
    environment = {
        "environment_id": environment_id,
        "host": socket.gethostname(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "protocol_version": PROTOCOL_VERSION,
    }
    try:
        info = ensure_fn()
    except Exception as exc:  # cache/probe failures must not stop the batch
        info = {"status": "missing", "value": None,
                "reason": f"ensure_failed({type(exc).__name__}: {exc})", "meta": {}}
    if not isinstance(info, Mapping):
        info = {"status": "missing", "value": None,
                "reason": "invalid_ensure_response", "meta": {}}
    status = info.get("status")
    reason = info.get("reason")
    value = info.get("value")
    if status == "ok" and (isinstance(value, bool)
                           or not isinstance(value, (int, float))
                           or not math.isfinite(value) or value <= 0):
        status, reason = "missing", "invalid_baseline_value"
    elif status not in ("ok", "missing", "stale"):
        status, reason = "missing", "invalid_baseline_status"
    baseline_mb = float(value) if status == "ok" else None
    record = {"status": status, "value": baseline_mb,
              "reason": reason if isinstance(reason, str) and reason else "reason_missing",
              "meta": dict(info["meta"]) if isinstance(info.get("meta"), Mapping) else {},
              "environment": environment}
    return record, baseline_mb


def run_offline_ours(
    task: Mapping[str, Any],
    frozen_input: Mapping[str, Any],
    *,
    mode: Mode,
    feedback_method: str,
    generate_candidate: CandidateGenerator,
    model: str,
    model_version: str,
    prompt_version: str,
    environment_id: str,
    generation_parameters: Mapping[str, Any] | None = None,
    frozen_manifest_path: str | Path | None = None,
    approved_frozen_sha256: str | None = None,
    bank_manifest_path: str | Path | None = None,
    approved_bank_manifest_sha256: str | None = None,
    provisional_list_path: str | Path | None = None,
    ensure_rss_baseline_fn: Callable[[], dict] = ensure_rss_baseline,
    measure_task_fn: Callable[..., dict] = measure_task,
    feedback_builder: Callable[..., dict] = build_feedback,
    write_diagnostic: Callable[[dict], Any] | None = None,
    write_result: Callable[[dict], Any] | None = None,
    allow_formal_results: bool = False,
    config: AcceptanceConfig = AcceptanceConfig(),
    rounds: int = 1,
    measurement_kwargs: Mapping[str, Any] | None = None,
) -> dict:
    """Run an injected loop and optionally hand records to injected writers.

    The default writes nothing. `allow_formal_results` is for a later approved
    runner; tests may exercise its gate with writers rooted in a temp directory.
    A JSONL task bank additionally requires B's frozen subset metadata and
    independently approved hashes of both files before formal_task is true.
    """
    for label, value in (("model", model), ("model_version", model_version),
                         ("prompt_version", prompt_version),
                         ("environment_id", environment_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
    if allow_formal_results and write_result is None:
        raise ValueError("allow_formal_results requires an injected write_result")
    frozen_verification = verify_frozen_task(
        task, frozen_input, manifest_path=frozen_manifest_path,
        approved_sha256=approved_frozen_sha256,
        bank_manifest_path=bank_manifest_path,
        approved_bank_manifest_sha256=approved_bank_manifest_sha256,
        provisional_list_path=provisional_list_path)
    rss_baseline_record, rss_baseline_mb = _rss_baseline(
        ensure_rss_baseline_fn, environment_id)
    measure_kwargs = dict(measurement_kwargs or {})
    if "model" in measure_kwargs or "model_version" in measure_kwargs:
        raise ValueError("model metadata must be passed through runner arguments")
    measure_kwargs.update({"model": model, "model_version": model_version})

    output = run_offline_loop(
        task, frozen_input, mode=mode, feedback_method=feedback_method,
        generate_candidate=generate_candidate, measure_task_fn=measure_task_fn,
        feedback_builder=feedback_builder, config=config, rounds=rounds,
        measurement_kwargs=measure_kwargs, rss_baseline_mb=rss_baseline_mb,
    )
    output.update({"model": model, "model_version": model_version,
                   "prompt_version": prompt_version,
                   "environment_id": environment_id,
                   "generation_parameters_requested": dict(generation_parameters or {}),
                   "frozen_task_verification": frozen_verification,
                   "rss_baseline_record": rss_baseline_record,
                   "rss_gate_status": ("eligible" if rss_baseline_mb is not None
                                       else "skipped"),
                   "mu_deducted_baseline_mb": (None if rss_baseline_mb is not None
                                               else "n/a"),
                   "formal_results_written": 0})
    for row in output["rounds"]:
        generation = row.get("generation") or {}
        row.update({"model": generation.get("model") or model,
                    "model_version": generation.get("actual_model_version") or model_version,
                    "actual_model_version": generation.get("actual_model_version"),
                    "model_version_source": ("provider_response" if generation.get("actual_model_version")
                                             else "declared_only"),
                    "declared_model": model,
                    "declared_model_version": model_version,
                    "prompt_version": prompt_version,
                    "generation_parameters_requested": dict(generation_parameters or {})})
        feedback = row["feedback"]
        measurement = row.get("candidate_measurement")
        if feedback_method.lower() == "f4" and isinstance(measurement, Mapping):
            hashes = row.get("prompt_hashes") or {}
            diagnostics = feedback.get("diagnostics") or {}
            effective = feedback.get("effective_method")
            proposed_method = "ours" if effective == "f4" else feedback.get("method")
            formal_task = frozen_verification["verified"]
            gate = check_f4_gate(
                diagnostics=diagnostics, feedback=row.get("feedback_text", ""),
                result_status=measurement.get("status"),
                harness_status=row.get("candidate_status"),
                prompt_hash_f3=hashes.get("f3"),
                prompt_hash_f4=hashes.get("f4"),
                formal_task=formal_task,
                method=proposed_method,
            )
            gate["formal_task"] = formal_task
            gate["formal_task_verification"] = frozen_verification["reason"]
            if not hashes.get("f3") or not hashes.get("f4"):
                gate["passed"] = False
                gate["failures"].append({"code": "G5", "detail":
                                         "same-code F3/F4 prompt hash unavailable: "
                                         + str(row.get("prompt_hash_error"))})
                gate["suggested_method"] = ("f3" if formal_task and
                    diagnostics.get("hotspot_mode") != "none" else
                    "f2" if formal_task else "diagnostic")
            c_complete = diagnostics.get("feedback_complete") is True
            gate["c_feedback_complete"] = c_complete
            row["gate"] = gate
            if (effective == "f4" and c_complete and gate["passed"]
                    and row.get("decision") == "accept"
                    and measurement.get("status") == "ok"):
                feedback.update({"method": "ours", "degrade_reason": None,
                                 "can_write_result": True,
                                 "schema_mapping_pending": False,
                                 "eligible_for_requested_method": True})
            elif effective in ("f4", "f4_line_hits"):
                # The candidate saw an F4 diagnostic prompt. Do not relabel it
                # as a formal F3 result; record the suggested rerun method.
                gate_codes = {item.get("code") for item in gate["failures"]}
                # G6 is a task provenance gate, not a failed hotspot diagnosis.
                # Keep the actual F4 feedback intact while forbidding a write.
                g6_only = effective == "f4" and c_complete and gate_codes == {"G6"}
                feedback.update({"method": None, "can_write_result": False,
                                 "degrade_reason": (None if g6_only else
                                     "budget_exceeded" if
                                     "space_feedback_omitted_by_length_limit" in diagnostics.get("reasons", [])
                                     else "no_hotspot")})
                if g6_only:
                    row["formal_block_reason"] = "G6_non_formal_task"
                row["suggested_rerun_method"] = gate["suggested_method"]
            elif not gate["passed"]:
                feedback["can_write_result"] = False
        blockers = []
        if not allow_formal_results:
            blockers.append("formal_write_disabled")
        if not frozen_verification["verified"]:
            blockers.append("frozen_task_unverified")
        if feedback_method.lower() != "f4" or feedback.get("method") != "ours":
            blockers.append("not_complete_ours")
        if feedback.get("requested_method") != "ours" or feedback.get("degrade_reason") is not None:
            blockers.append("degraded_or_wrong_requested_method")
        if not feedback.get("can_write_result") or not (row.get("gate") or {}).get("passed"):
            blockers.append("feedback_or_gate_blocked")
        if row.get("decision") != "accept" or not isinstance(measurement, Mapping) or measurement.get("status") != "ok":
            blockers.append("candidate_not_accepted_ok")
        if generation.get("status") != "ok":
            blockers.append("actual_model_response_unavailable")
        if isinstance(measurement, Mapping):
            expected = (task["task_id"], frozen_input["tier"], frozen_input["args_hash"])
            if _pair_reason(row.get("baseline_measurement") or {}, measurement, expected):
                blockers.append("frozen_result_pair_mismatch")
            manifest = measurement.get("manifest") or {}
            if (not isinstance(manifest, Mapping) or
                    manifest.get("model") != generation.get("model") or
                    manifest.get("model_version") != generation.get("actual_model_version")):
                blockers.append("actual_model_manifest_mismatch")
            if isinstance(manifest, Mapping) and any(
                    manifest.get(key) != generation.get("parameters", {}).get(key)
                    for key in ("temperature", "seed")
                    if key in generation.get("parameters", {})):
                blockers.append("generation_parameters_manifest_mismatch")
        row["formal_write_blockers"] = blockers
        if not blockers:
            formal = dict(measurement)
            formal["method"] = feedback["method"]
            formal["requested_method"] = feedback["requested_method"]
            formal["degrade_reason"] = feedback["degrade_reason"]
            write_result(formal)
            row["formal_result_written"] = True
            output["formal_results_written"] += 1
    output["formal_result_written"] = output["formal_results_written"] > 0
    if write_diagnostic is not None:
        write_diagnostic(output)
    return output
