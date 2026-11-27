"""Small offline Ours loop with injected generation and measurement.

This module writes no files and never calls a model by itself. It returns
diagnostic-demo records only; even an accepted candidate is not a formal
Result. A later runner must separately handle persistence and schema gates.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from eval.harness import measure_task
from optimize.acceptance import AcceptanceConfig, Mode, decide_acceptance
from optimize.feedback import build_feedback


def _feedback_projection(feedback: Mapping[str, Any]) -> dict:
    """Keep C's schema method separate from its diagnostic effective arm."""
    if (not isinstance(feedback.get("prompt"), str)
            or not isinstance(feedback.get("requested_method"), str)
            or not isinstance(feedback.get("effective_method"), str)
            or not isinstance(feedback.get("can_write_result"), bool)
            or "degrade_reason" not in feedback):
        raise ValueError("feedback_builder must return C's prompt and method metadata")
    if feedback["can_write_result"] and not isinstance(feedback.get("method"), str):
        raise ValueError("writable feedback requires a schema method")
    return {key: feedback.get(key) for key in (
        "method", "requested_method", "effective_method", "requested_arm",
        "effective_arm", "degrade_reason", "schema_mapping_pending",
        "eligible_for_requested_method", "can_write_result", "diagnostics",
    )}


def _frozen_plan(task: Mapping[str, Any], frozen_input: Mapping[str, Any]) -> dict:
    inputs = task.get("inputs")
    if not isinstance(inputs, list) or not any(
        isinstance(item, Mapping) and dict(item) == dict(frozen_input) for item in inputs
    ):
        raise ValueError("frozen_input must be one of Task.inputs")
    if (not isinstance(frozen_input.get("tier"), str) or not frozen_input["tier"]
            or not isinstance(frozen_input.get("args_hash"), str)
            or not frozen_input["args_hash"]):
        raise ValueError("frozen_input requires a tier and nonempty args_hash")
    test = frozen_input.get("test", task.get("test"))
    if not isinstance(test, str) or not test.strip():
        raise ValueError("frozen_input requires a usable test block")
    return {**dict(frozen_input), "test": test}


def run_offline_loop(
    task: Mapping[str, Any],
    frozen_input: Mapping[str, Any],
    *,
    mode: Mode,
    feedback_method: str,
    generate_candidate: Callable[[str, str, int], str],
    measure_task_fn: Callable[..., dict] = measure_task,
    feedback_builder: Callable[..., dict] = build_feedback,
    config: AcceptanceConfig = AcceptanceConfig(),
    rounds: int = 1,
    measurement_kwargs: Mapping[str, Any] | None = None,
) -> dict:
    """Connect feedback, mock candidate, shared measurement, and acceptance.

    generate_candidate(prompt, current_code, round_index) returns full code.
    measure_task_fn has eval.harness.measure_task's signature. Every call
    receives the same one-element tier_plan, with no candidate-specific tier
    escalation. The return value contains decisions, not formal Results.
    """
    if not isinstance(task.get("code"), str) or not task["code"]:
        raise ValueError("Task.code is required")
    if not isinstance(task.get("description"), str) or not task["description"].strip():
        raise ValueError("Task.description is required")
    if mode not in ("time-only", "mem-only", "joint"):
        raise ValueError("mode must be time-only, mem-only, or joint")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    plan = _frozen_plan(task, frozen_input)
    kwargs = dict(measurement_kwargs or {})
    if {"task", "code", "method", "tier_plan"} & kwargs.keys():
        raise ValueError("measurement_kwargs cannot override task/code/method/tier_plan")

    initial_code = task["code"]
    current_code = initial_code
    current_measure = measure_task_fn(
        task, code=None, method="base", tier_plan=[dict(plan)], **kwargs
    )
    output = {
        "kind": "diagnostic_demo",
        "formal_result_written": False,
        "task_id": task.get("task_id"),
        "mode": mode,
        "feedback_method": feedback_method,
        "frozen_input": {"tier": plan["tier"], "args_hash": plan["args_hash"]},
        "initial_code": initial_code,
        "current_code": current_code,
        "baseline_status": current_measure.get("status"),
        "rounds": [],
    }
    if current_measure.get("status") != "ok":
        output["stop_reason"] = "baseline_status_" + str(current_measure.get("status"))
        return output

    for index in range(rounds):
        time = current_measure.get("time") or {}
        feedback = feedback_builder(
            feedback_method, task, current_code,
            total_ms=time.get("mean_trimmed_ms"),
        )
        if not isinstance(feedback, dict):
            raise ValueError("feedback_builder must return a feedback dict")
        feedback_meta = _feedback_projection(feedback)
        formal_eligible = feedback_meta["can_write_result"]
        candidate_code = generate_candidate(feedback["prompt"], current_code, index)
        if not isinstance(candidate_code, str) or not candidate_code.strip():
            output["rounds"].append({
                "round": index, "decision": "reject", "reason": "empty_candidate",
                "feedback": feedback_meta,
            })
            continue
        if candidate_code == current_code:
            output["rounds"].append({
                "round": index, "decision": "reject", "reason": "unchanged_candidate",
                "feedback": feedback_meta,
            })
            continue
        # "diagnostic" is intentionally outside the frozen method enum. Its
        # in-memory Result must never be persisted while C's F4 is incomplete.
        actual_method = feedback_meta["method"] if formal_eligible else "diagnostic"
        candidate_measure = measure_task_fn(
            task, code=candidate_code, method=actual_method,
            tier_plan=[dict(plan)], **kwargs
        )
        decision = decide_acceptance(current_measure, candidate_measure, mode, config)
        round_record = {
            "round": index,
            "candidate_code": candidate_code,
            "candidate_status": candidate_measure.get("status"),
            "decision": decision["decision"],
            "reason": decision["reason"],
            "decision_detail": decision,
            "feedback": feedback_meta,
            "formal_result_written": False,
        }
        output["rounds"].append(round_record)
        if decision["decision"] == "retest":
            # Keep both unmodified harness Results for a later, explicit retest.
            # A pending decision must not seed another candidate generation.
            round_record["baseline_measurement"] = current_measure
            round_record["candidate_measurement"] = candidate_measure
            output["pending_retest"] = round_record
            output["stop_reason"] = "pending_retest"
            break
        if decision["accepted"]:
            current_code = candidate_code
            current_measure = candidate_measure
    output["current_code"] = current_code
    return output
