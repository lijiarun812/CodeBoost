"""Small offline Ours loop with injected generation and measurement.

This module writes no files and never calls a model by itself. It returns
diagnostic-demo records only; even an accepted candidate is not a formal
Result. A later runner must separately handle persistence and schema gates.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from eval.harness import measure_task
from eval.space import pareto_accept
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


def _pair_reason(baseline: Mapping[str, Any], candidate: Mapping[str, Any],
                 expected: tuple[str, str, str]) -> str | None:
    """A's Pareto rule does not verify frozen task/tier/args_hash pairing."""
    def key(result: Mapping[str, Any]) -> tuple | None:
        time = result.get("time") or {}
        tiers = time.get("tiers") if isinstance(time, Mapping) else None
        if not isinstance(tiers, list) or not tiers or not isinstance(tiers[-1], Mapping):
            return None
        last = tiers[-1]
        values = (result.get("task_id"), last.get("tier"), last.get("args_hash"))
        return values if all(isinstance(value, str) and value for value in values) else None

    base_key, candidate_key = key(baseline), key(candidate)
    if base_key is None or candidate_key is None:
        return "pair_unavailable"
    return "pair_mismatch" if base_key != candidate_key or base_key != expected else None


def _time_missing(result: Mapping[str, Any]) -> bool:
    time = result.get("time") or {}
    return not isinstance(time, Mapping) or time.get("mean_trimmed_ms") is None


def _peak_missing(result: Mapping[str, Any]) -> bool:
    mem = result.get("mem") or {}
    return not isinstance(mem, Mapping) or mem.get("peak_tracemalloc_mb") is None


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
    rss_baseline_mb: float | None = None,
) -> dict:
    """Connect feedback, mock candidate, shared measurement, and acceptance.

    generate_candidate(prompt, current_code, round_index) returns full code.
    measure_task_fn has eval.harness.measure_task's signature. Every call
    receives the same one-element tier_plan, with no candidate-specific tier
    escalation. The return value contains decisions, not formal Results.
    """
    if not isinstance(task.get("code"), str) or not task["code"]:
        raise ValueError("Task.code is required")
    if not isinstance(task.get("task_id"), str) or not task["task_id"]:
        raise ValueError("Task.task_id is required")
    if not isinstance(task.get("description"), str) or not task["description"].strip():
        raise ValueError("Task.description is required")
    if mode not in ("time-only", "mem-only", "joint"):
        raise ValueError("mode must be time-only, mem-only, or joint")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        raise ValueError("rounds must be a positive integer")
    plan = _frozen_plan(task, frozen_input)
    expected_pair = (task["task_id"], plan["tier"], plan["args_hash"])
    kwargs = dict(measurement_kwargs or {})
    if {"task", "code", "method", "tier_plan"} & kwargs.keys():
        raise ValueError("measurement_kwargs cannot override task/code/method/tier_plan")

    initial_code = task["code"]
    current_code = initial_code
    current_method = "base"
    current_measure = measure_task_fn(
        task, code=None, method="base", tier_plan=[dict(plan)], **kwargs
    )
    output = {
        "kind": "diagnostic_demo",
        "formal_result_written": False,
        "task_id": task.get("task_id"),
        "mode": mode,
        "feedback_method": feedback_method,
        "acceptance_rule": ("eval.space.pareto_accept:v1.1" if mode == "joint"
                            else "optimize.acceptance.decide_acceptance:group"),
        "rss_baseline_mb": rss_baseline_mb if mode == "joint" else None,
        "frozen_input": {"tier": plan["tier"], "args_hash": plan["args_hash"]},
        "initial_code": initial_code,
        "current_code": current_code,
        "baseline_status": current_measure.get("status"),
        "rounds": [],
    }
    if current_measure.get("status") != "ok":
        if mode == "joint":
            output["stop_reason"] = "incomparable"
            output["incomparable_reason"] = "baseline_status_" + str(current_measure.get("status"))
        else:
            output["stop_reason"] = "baseline_status_" + str(current_measure.get("status"))
        return output
    baseline_pair_issue = _pair_reason(current_measure, current_measure, expected_pair)
    if baseline_pair_issue:
        output["stop_reason"] = "incomparable"
        output["incomparable_reason"] = baseline_pair_issue
        return output

    for index in range(rounds):
        baseline_retest = None
        if mode == "joint" and _time_missing(current_measure):
            previous = current_measure
            current_measure = measure_task_fn(
                task, code=None if current_code == initial_code else current_code,
                method=current_method, tier_plan=[dict(plan)], **kwargs,
            )
            baseline_retest = {"side": "baseline", "reason": "time_missing",
                               "before": previous, "after": current_measure}
            if current_measure.get("status") != "ok" or _time_missing(current_measure):
                output["stop_reason"] = "incomparable"
                output["incomparable_reason"] = "baseline_time_unavailable_after_retest"
                output["baseline_retest"] = baseline_retest
                break
        current_pair_issue = _pair_reason(current_measure, current_measure, expected_pair)
        if current_pair_issue:
            output["stop_reason"] = "incomparable"
            output["incomparable_reason"] = current_pair_issue
            if baseline_retest is not None:
                output["baseline_retest"] = baseline_retest
            break
        time = current_measure.get("time") or {}
        feedback = feedback_builder(
            feedback_method, task, current_code,
            total_ms=time.get("mean_trimmed_ms"),
            space_result=current_measure,
            rss_baseline_mb=rss_baseline_mb,
        )
        if not isinstance(feedback, dict):
            raise ValueError("feedback_builder must return a feedback dict")
        feedback_meta = _feedback_projection(feedback)
        formal_eligible = feedback_meta["can_write_result"]
        candidate_code = generate_candidate(feedback["prompt"], current_code, index)
        if not isinstance(candidate_code, str) or not candidate_code.strip():
            output["rounds"].append({
                "round": index, "decision": "reject", "reason": "empty_candidate",
                "acceptance_rule": output["acceptance_rule"],
                "source_code": current_code, "prompt": feedback["prompt"],
                "feedback": feedback_meta,
            })
            continue
        if candidate_code == current_code:
            output["rounds"].append({
                "round": index, "decision": "reject", "reason": "unchanged_candidate",
                "acceptance_rule": output["acceptance_rule"],
                "source_code": current_code, "prompt": feedback["prompt"],
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
        retest_record = baseline_retest
        if mode == "joint":
            retests_used = 1 if baseline_retest is not None else 0
            if candidate_measure.get("status") == "incorrect":
                decision = pareto_accept(current_measure, candidate_measure,
                                         rss_baseline_mb=rss_baseline_mb,
                                         retests_used=retests_used, retest_budget=1)
            else:
                mismatch = _pair_reason(current_measure, candidate_measure, expected_pair)
                decision = ({"decision": "reject", "reasons": [mismatch],
                             "pair_reason": mismatch} if mismatch else
                            pareto_accept(current_measure, candidate_measure,
                                          rss_baseline_mb=rss_baseline_mb,
                                          retests_used=retests_used, retest_budget=1))
            if decision["decision"] == "retest":
                side = ("baseline" if decision.get("retest_reason") == "peak_missing"
                        and _peak_missing(current_measure) else "candidate")
                before = current_measure if side == "baseline" else candidate_measure
                if side == "baseline":
                    current_measure = measure_task_fn(
                        task, code=None if current_code == initial_code else current_code,
                        method=current_method, tier_plan=[dict(plan)], **kwargs,
                    )
                else:
                    candidate_measure = measure_task_fn(
                        task, code=candidate_code, method=actual_method,
                        tier_plan=[dict(plan)], **kwargs,
                    )
                after = current_measure if side == "baseline" else candidate_measure
                retest_record = {"side": side, "reason": decision.get("retest_reason"),
                                 "before": before, "after": after}
                mismatch = _pair_reason(current_measure, candidate_measure, expected_pair)
                decision = ({"decision": "reject", "reasons": [mismatch],
                             "pair_reason": mismatch} if mismatch else
                            pareto_accept(current_measure, candidate_measure,
                                          rss_baseline_mb=rss_baseline_mb,
                                          retests_used=1, retest_budget=1))
        else:
            mismatch = (_pair_reason(current_measure, candidate_measure, expected_pair)
                        if candidate_measure.get("status") == "ok" else None)
            decision = ({"decision": "reject", "accepted": False,
                         "reason": mismatch, "pair_reason": mismatch} if mismatch else
                        decide_acceptance(current_measure, candidate_measure, mode, config))
        round_record = {
            "round": index,
            "acceptance_rule": output["acceptance_rule"],
            "source_code": current_code,
            "prompt": feedback["prompt"],
            "candidate_code": candidate_code,
            "candidate_status": candidate_measure.get("status"),
            "decision": decision["decision"],
            "reason": decision.get("reason") or "; ".join(decision.get("reasons", [])),
            "decision_detail": decision,
            "baseline_measurement": current_measure,
            "candidate_measurement": candidate_measure,
            "retest": retest_record,
            "space_improvement_claimed": (
                decision.get("space_better") is True if mode == "joint" else None
            ),
            "feedback": feedback_meta,
            "formal_result_written": False,
        }
        output["rounds"].append(round_record)
        if decision["decision"] == "retest":
            # Keep both unmodified harness Results for a later, explicit retest.
            # A pending decision must not seed another candidate generation.
            output["pending_retest"] = round_record
            output["stop_reason"] = "pending_retest"
            break
        if decision["decision"] == "incomparable":
            output["stop_reason"] = "incomparable"
            break
        if decision.get("accepted", decision["decision"] == "accept"):
            current_code = candidate_code
            current_measure = candidate_measure
            current_method = actual_method
    output["current_code"] = current_code
    return output
