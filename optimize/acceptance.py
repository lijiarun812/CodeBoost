"""Pure, local acceptance decisions for the optimization track.

All thresholds below are this group's proposed policy, not paper claims.
Inputs are two eval.harness.measure_task Result dictionaries. This module
does not measure code, change Results, read files, or persist decisions.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

Mode = Literal["time-only", "mem-only", "joint"]


@dataclass(frozen=True)
class AcceptanceConfig:
    """Group policy, expressed as fractions unless a unit is in the name."""

    min_time_gain: float = 0.05
    min_memory_gain: float = 0.10
    max_other_regression: float = 0.10
    max_cv: float = 0.10
    noise_multiplier: float = 2.0
    threshold_margin: float = 0.01
    min_heap_mb: float = 0.01
    max_rss_mb: float | None = 2048.0

    def __post_init__(self) -> None:
        fractions = (
            self.min_time_gain, self.min_memory_gain,
            self.max_other_regression, self.max_cv, self.threshold_margin,
        )
        if any(not _finite(v) or not 0 <= v < 1 for v in fractions):
            raise ValueError("fraction thresholds must be finite and in [0, 1)")
        if not _finite(self.noise_multiplier) or self.noise_multiplier < 0:
            raise ValueError("noise_multiplier must be finite and nonnegative")
        if not _finite(self.min_heap_mb) or self.min_heap_mb <= 0:
            raise ValueError("min_heap_mb must be finite and positive")
        if self.max_rss_mb is not None and (
            not _finite(self.max_rss_mb) or self.max_rss_mb <= 0
        ):
            raise ValueError("max_rss_mb must be finite and positive or None")


def _finite(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _selected_tier(result: Mapping[str, Any]) -> dict:
    time = result.get("time")
    tiers = time.get("tiers") if isinstance(time, Mapping) else None
    if not isinstance(tiers, list) or not tiers or not isinstance(tiers[-1], Mapping):
        return {}
    return dict(tiers[-1])


def _raw(result: Mapping[str, Any]) -> dict:
    time = result.get("time")
    mem = result.get("mem")
    time = time if isinstance(time, Mapping) else {}
    mem = mem if isinstance(mem, Mapping) else {}
    tier = _selected_tier(result)
    return {
        "status": result.get("status"),
        "task_id": result.get("task_id"),
        "tier": tier.get("tier"),
        "args_hash": tier.get("args_hash"),
        "tier_status": tier.get("status"),
        "mean_trimmed_ms": time.get("mean_trimmed_ms"),
        "cv": time.get("cv"),
        "peak_tracemalloc_mb": mem.get("peak_tracemalloc_mb"),
        "peak_rss_mb": mem.get("peak_rss_mb"),
        "downgraded": result.get("downgraded"),
    }


def decide_acceptance(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    mode: Mode,
    config: AcceptanceConfig = AcceptanceConfig(),
) -> dict:
    """Return decision, reason, raw readings, derived gains, and policy.

    decision is accept, reject, or retest. A positive gain means improvement.
    Pareto dominance means no measured regression in either time or heap
    peak and a strict improvement in at least one; acceptance additionally
    requires the configured gain/noise gates.
    """
    if mode not in ("time-only", "mem-only", "joint"):
        raise ValueError("mode must be time-only, mem-only, or joint")
    if not isinstance(config, AcceptanceConfig):
        raise TypeError("config must be AcceptanceConfig")
    raw = {"baseline": _raw(baseline), "candidate": _raw(candidate)}
    b, c = raw["baseline"], raw["candidate"]
    derived = {"time_gain_fraction": None, "memory_gain_fraction": None,
               "effective_time_threshold": None, "time_reliable": False,
               "pareto_dominates": None}

    def finish(decision: str, reason: str, relationship: str | None = None) -> dict:
        return {"decision": decision, "accepted": decision == "accept",
                "reason": reason, "relationship": relationship,
                "mode": mode, "raw": raw, "derived": derived,
                "thresholds": asdict(config)}

    for side, row in (("baseline", b), ("candidate", c)):
        if row["status"] != "ok":
            return finish("reject", f"{side}_status_{row['status'] or 'missing'}")
    for row in (b, c):
        if (not isinstance(row["task_id"], str) or not row["task_id"]
                or not isinstance(row["tier"], str) or not row["tier"]
                or not isinstance(row["args_hash"], str) or not row["args_hash"]
                or row["tier_status"] != "ok"):
            return finish("reject", "pair_unavailable")
    if (b["task_id"], b["tier"], b["args_hash"]) != (
        c["task_id"], c["tier"], c["args_hash"]
    ):
        return finish("reject", "pair_mismatch")

    rss = c["peak_rss_mb"]
    if config.max_rss_mb is not None:
        if not _finite(rss) or rss <= 0:
            return finish("retest", "candidate_rss_unavailable")
        if rss > config.max_rss_mb:
            return finish("reject", "candidate_rss_limit_exceeded")
    time_values_ok = all(
        _finite(row["mean_trimmed_ms"]) and row["mean_trimmed_ms"] > 0
        for row in (b, c)
    )
    cv_values_ok = all(
        _finite(row["cv"]) and row["cv"] >= 0 for row in (b, c)
    )
    if mode != "mem-only":
        for side, row in (("baseline", b), ("candidate", c)):
            if not _finite(row["mean_trimmed_ms"]) or row["mean_trimmed_ms"] <= 0:
                return finish("retest", f"{side}_time_unmeasurable")
            if not _finite(row["cv"]) or row["cv"] < 0:
                return finish("retest", f"{side}_cv_unavailable")
        if b["cv"] > config.max_cv or c["cv"] > config.max_cv:
            return finish("retest", "time_noise_high")
    time_reliable = (time_values_ok and cv_values_ok
                     and b["cv"] <= config.max_cv and c["cv"] <= config.max_cv)
    derived["time_reliable"] = time_reliable
    time_gain = (1.0 - c["mean_trimmed_ms"] / b["mean_trimmed_ms"]
                 if time_values_ok else None)
    if time_gain is not None:
        derived["time_gain_fraction"] = time_gain
    time_threshold = (max(config.min_time_gain,
                          config.noise_multiplier * max(b["cv"], c["cv"]))
                      if cv_values_ok else None)
    derived["effective_time_threshold"] = time_threshold

    heap_valid = all(
        _finite(row["peak_tracemalloc_mb"])
        and row["peak_tracemalloc_mb"] >= config.min_heap_mb
        for row in (b, c)
    )
    if heap_valid:
        memory_gain = 1.0 - c["peak_tracemalloc_mb"] / b["peak_tracemalloc_mb"]
        derived["memory_gain_fraction"] = memory_gain
        if time_reliable:
            derived["pareto_dominates"] = (
                time_gain >= 0 and memory_gain >= 0
                and (time_gain > 0 or memory_gain > 0)
            )
    else:
        memory_gain = None

    margin = config.threshold_margin
    if mode == "time-only":
        if abs(time_gain - time_threshold) <= margin:
            return finish("retest", "time_near_threshold")
        if time_gain > time_threshold:
            reason = ("accepted_time_gain_with_memory_regression"
                      if memory_gain is not None and memory_gain < 0
                      else "accepted_time_gain")
            return finish("accept", reason)
        return finish("reject", "time_gain_below_threshold")

    if memory_gain is None:
        return finish("retest", "memory_unmeasurable")
    if mode == "mem-only":
        if abs(memory_gain - config.min_memory_gain) <= margin:
            return finish("retest", "memory_near_threshold")
        if memory_gain > config.min_memory_gain:
            reason = ("accepted_memory_gain_time_uncertain" if not time_reliable
                      else "accepted_memory_gain_with_time_regression"
                      if time_gain < 0 else "accepted_memory_gain")
            return finish("accept", reason)
        return finish("reject", "memory_gain_below_threshold")

    if time_gain < 0 and memory_gain < 0:
        return finish("reject", "candidate_dominated", "baseline_dominates")
    regression_limit = -config.max_other_regression
    time_path = time_gain > time_threshold + margin and memory_gain > regression_limit + margin
    memory_path = (memory_gain > config.min_memory_gain + margin
                   and time_gain > regression_limit + margin)
    if time_path or memory_path:
        if derived["pareto_dominates"]:
            return finish("accept", "accepted_pareto_dominance", "pareto_dominates")
        if time_path and memory_gain < 0:
            return finish("accept", "accepted_time_gain_with_memory_regression",
                          "bounded_tradeoff")
        return finish("accept", "accepted_memory_gain_with_time_regression",
                      "bounded_tradeoff")
    near_time_path = (
        time_gain >= time_threshold - margin
        and memory_gain >= regression_limit - margin
        and (abs(time_gain - time_threshold) <= margin
             or abs(memory_gain - regression_limit) <= margin)
    )
    near_memory_path = (
        memory_gain >= config.min_memory_gain - margin
        and time_gain >= regression_limit - margin
        and (abs(memory_gain - config.min_memory_gain) <= margin
             or abs(time_gain - regression_limit) <= margin)
    )
    if near_time_path or near_memory_path:
        return finish("retest", "joint_near_threshold",
                      "pareto_dominates" if derived["pareto_dominates"] else "tradeoff")
    if time_gain > time_threshold and memory_gain < regression_limit:
        return finish("reject", "memory_regression_too_large", "tradeoff")
    if memory_gain > config.min_memory_gain and time_gain < regression_limit:
        return finish("reject", "time_regression_too_large", "tradeoff")
    return finish("reject", "joint_gain_below_threshold",
                  "pareto_dominates" if derived["pareto_dominates"] else "tradeoff")
