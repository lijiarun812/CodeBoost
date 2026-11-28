"""Offline validator for the unified comparison preregistration template.

It never imports a model client, reads keys, measures code, or writes Results.
Run ``python -m optimize.control_config`` to list remaining trial blockers.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from eval.harness import PROTOCOL_VERSION

ARMS = ("G0", "G1", "G2", "G3", "F0", "F1", "F2", "F3", "F4")
DEFAULT = Path(__file__).with_name("unified_control.json")


def check_control(config: Mapping[str, Any]) -> dict:
    """Validate one shared run definition; return readiness and blockers."""
    if config.get("schema_version") != "unified-control-draft-v1":
        raise ValueError("unsupported control config version")
    prompt = config.get("prompt") or {}
    arms = prompt.get("arms") or {}
    if (prompt.get("common_template") != "optimize.prompts.render_prompt"
            or set(arms) != set(ARMS)):
        raise ValueError("all nine arms must share optimize.prompts.render_prompt")
    if any(not isinstance(arms[name], Mapping) or
           set(arms[name]) != {"feedback", "shared_harness_builder"}
           for name in ARMS):
        raise ValueError("arm-specific generation or measurement overrides are forbidden")
    measure = config.get("measurement") or {}
    if (measure.get("harness") != "eval.harness.measure_task"
            or measure.get("protocol_version") != PROTOCOL_VERSION
            or measure.get("tier_plan") != "one_frozen_tier_per_task"
            or measure.get("pairing") != ["task_id", "tier", "args_hash"]
            or measure.get("time_metric") != "mean_trimmed_ms"
            or measure.get("heap_metric") != "peak_tracemalloc_mb"
            or measure.get("rss_role") != "resource_gate_only"
            or measure.get("diagnostic_line_time_in_result") is not False):
        raise ValueError("shared harness or measurement contract differs")
    model = config.get("model") or {}
    if (isinstance(model.get("temperature"), bool) or
            not isinstance(model.get("temperature"), (int, float)) or
            not math.isfinite(model["temperature"]) or
            not 0 <= model["temperature"] <= 2 or
            isinstance(model.get("seed"), bool) or not isinstance(model.get("seed"), int) or
            isinstance(model.get("generations_per_task_arm"), bool) or
            not isinstance(model.get("generations_per_task_arm"), int) or
            model["generations_per_task_arm"] < 1 or
            isinstance(model.get("max_tokens"), bool) or
            not isinstance(model.get("max_tokens"), int) or model["max_tokens"] < 1 or
            not isinstance(model.get("thinking"), bool) or
            isinstance(model.get("request_timeout_s"), bool) or
            not isinstance(model.get("request_timeout_s"), (int, float)) or
            not math.isfinite(model["request_timeout_s"]) or
            model["request_timeout_s"] <= 0):
        raise ValueError("generation parameters must be explicit and valid")
    bank = config.get("task_bank") or {}
    selected = bank.get("frozen_task_tiers") or []
    if not isinstance(selected, list) or any(
            not isinstance(item, Mapping) or
            not all(isinstance(item.get(key), str) and item[key]
                    for key in ("task_id", "tier", "args_hash"))
            or not re.fullmatch(r"sha256:[0-9a-f]{16}", item["args_hash"])
            for item in selected):
        raise ValueError("each selected task needs task_id, tier, args_hash")
    if len({item["task_id"] for item in selected}) != len(selected):
        raise ValueError("each task must have exactly one frozen tier")
    blockers = []
    if not selected or not all(bank.get(key) for key in (
            "frozen_tasks_path", "frozen_manifest_path", "approved_tasks_sha256",
            "approved_manifest_sha256")):
        blockers.append("b_frozen_bank_and_pinned_hashes_missing")
    if not all(model.get(key) for key in ("provider", "model_id", "actual_version")):
        blockers.append("model_and_actual_version_unset")
    if not prompt.get("version"):
        blockers.append("common_prompt_version_unset")
    if any(arms[name]["shared_harness_builder"] is None for name in ARMS[:4]):
        blockers.append("g0_g3_shared_harness_adapters_missing")
    for key in ("max_total_calls", "max_total_tokens", "max_total_cost_usd"):
        value = model.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0):
            blockers.append("generation_budget_unset")
            break
    else:
        needed_calls = len(ARMS) * len(selected) * model["generations_per_task_arm"]
        if model["max_total_calls"] < needed_calls:
            blockers.append("call_budget_below_planned_generations")
    if measure.get("correctness") != "same_frozen_test_plus_independent_holdout":
        blockers.append("independent_correctness_holdout_unfrozen")
    ready = not blockers
    if (config.get("run_enabled") or config.get("formal_results_enabled")) and not ready:
        raise ValueError("execution cannot be enabled while controls are unresolved")
    return {"ready_for_model_trial": ready, "blockers": blockers,
            "arm_count": len(ARMS), "formal_results_enabled": bool(config.get("formal_results_enabled"))}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT)
    args = parser.parse_args()
    report = check_control(json.loads(args.path.read_text(encoding="utf-8")))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
