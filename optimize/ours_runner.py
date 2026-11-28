"""Injectable offline Ours runner; no model client or file path is built in.

Formal Result writes are disabled by default. The batch entry checks A's RSS
baseline through eval.rss_baseline.ensure(); generation, measurement, and
writers remain injectable. F4 stays diagnostic-only unless C completeness and
A's executable gate both pass; writes remain disabled by default.
"""

from __future__ import annotations

import math
import socket
import sys
from typing import Any, Callable, Mapping

from eval.gate import check as check_f4_gate
from eval.harness import PROTOCOL_VERSION, measure_task
from eval.rss_baseline import ensure as ensure_rss_baseline
from optimize.acceptance import AcceptanceConfig, Mode
from optimize.feedback import build_feedback
from optimize.ours_pipeline import run_offline_loop


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
    generate_candidate: Callable[[str, str, int], str],
    model: str,
    model_version: str,
    prompt_version: str,
    environment_id: str,
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
    """
    for label, value in (("model", model), ("model_version", model_version),
                         ("prompt_version", prompt_version),
                         ("environment_id", environment_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
    if allow_formal_results and write_result is None:
        raise ValueError("allow_formal_results requires an injected write_result")
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
                   "rss_baseline_record": rss_baseline_record,
                   "rss_gate_status": ("eligible" if rss_baseline_mb is not None
                                       else "skipped"),
                   "mu_deducted_baseline_mb": (None if rss_baseline_mb is not None
                                               else "n/a"),
                   "formal_results_written": 0})
    for row in output["rounds"]:
        row.update({"model": model, "model_version": model_version,
                    "prompt_version": prompt_version})
        feedback = row["feedback"]
        measurement = row.get("candidate_measurement")
        if feedback_method.lower() == "f4" and isinstance(measurement, Mapping):
            hashes = row.get("prompt_hashes") or {}
            diagnostics = feedback.get("diagnostics") or {}
            effective = feedback.get("effective_method")
            proposed_method = "ours" if effective == "f4" else feedback.get("method")
            # B's frozen task list and input hash have not been verified here.
            # A task_id or caller-provided Task alone cannot establish formality.
            formal_task = False
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
            gate["formal_task_verification"] = "b_frozen_task_and_input_hash_not_verified"
            if not hashes.get("f3") or not hashes.get("f4"):
                gate["passed"] = False
                gate["failures"].append({"code": "G5", "detail":
                                         "same-code F3/F4 prompt hash unavailable: "
                                         + str(row.get("prompt_hash_error"))})
                gate["suggested_method"] = "diagnostic"
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
                feedback.update({"method": None, "can_write_result": False,
                                 "degrade_reason": ("budget_exceeded" if
                                     "space_feedback_omitted_by_length_limit" in diagnostics.get("reasons", [])
                                     else "no_hotspot")})
                row["suggested_rerun_method"] = gate["suggested_method"]
            elif not gate["passed"]:
                feedback["can_write_result"] = False
        if (allow_formal_results and row.get("decision") == "accept"
                and feedback["can_write_result"]
                and isinstance(measurement, Mapping)
                and measurement.get("status") == "ok"):
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
