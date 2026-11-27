"""Injectable offline Ours runner; no model client or file path is built in.

Formal Result writes are disabled by default. Callers supply the measurement
environment's own RSS baseline record, candidate generator, harness, and
optional writers. Incomplete F4 is always diagnostic-only.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from eval.harness import measure_task
from optimize.acceptance import AcceptanceConfig, Mode
from optimize.feedback import build_feedback
from optimize.ours_pipeline import run_offline_loop


def _rss_baseline(record: Mapping[str, Any] | None, environment_id: str,
                  mode: Mode) -> float | None:
    if mode != "joint":
        return None
    if not isinstance(record, Mapping) or record.get("environment_id") != environment_id:
        raise ValueError("joint mode requires an RSS baseline record for this environment")
    value = record.get("peak_rss_mb")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("RSS baseline record requires positive peak_rss_mb")
    return float(value)


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
    rss_baseline_record: Mapping[str, Any] | None = None,
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
    rss_baseline_mb = _rss_baseline(rss_baseline_record, environment_id, mode)
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
                   "rss_baseline_record": (dict(rss_baseline_record)
                                           if rss_baseline_record is not None else None),
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
