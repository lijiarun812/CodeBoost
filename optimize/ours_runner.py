"""Injectable offline Ours runner; no model client or file path is built in.

Formal Result writes are disabled by default. The batch entry checks A's RSS
baseline through eval.rss_baseline.ensure(); generation, measurement, and
writers remain injectable. Incomplete F4 is always diagnostic-only.
"""

from __future__ import annotations

import math
import socket
import sys
from typing import Any, Callable, Mapping

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
        if (allow_formal_results and feedback["can_write_result"]
                and isinstance(measurement, Mapping)):
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
