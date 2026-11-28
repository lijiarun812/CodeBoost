"""Offline checks for the unified comparison preregistration template."""

from __future__ import annotations

import copy
import json
import unittest

from optimize.control_config import DEFAULT, check_control


class ControlConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(DEFAULT.read_text(encoding="utf-8"))

    def test_checked_in_template_is_safe_and_reports_real_blockers(self):
        report = check_control(self.config)
        self.assertFalse(report["ready_for_model_trial"])
        self.assertFalse(report["formal_results_enabled"])
        self.assertEqual(report["arm_count"], 9)
        self.assertIn("b_frozen_bank_and_pinned_hashes_missing", report["blockers"])
        self.assertIn("g0_g3_shared_harness_adapters_missing", report["blockers"])
        self.assertIn("model_and_actual_version_unset", report["blockers"])
        self.assertIn("frozen_tasks_path", self.config["task_bank"])
        self.assertNotIn("jsonl_path", self.config["task_bank"])

    def test_arm_specific_generation_or_measurement_overrides_are_rejected(self):
        for arm, extra in (("G0", {"temperature": 0.7}),
                           ("F4", {"tier": "t3"})):
            with self.subTest(arm=arm):
                cfg = copy.deepcopy(self.config)
                cfg["prompt"]["arms"][arm].update(extra)
                with self.assertRaisesRegex(ValueError, "arm-specific"):
                    check_control(cfg)

    def test_unresolved_controls_cannot_enable_model_or_formal_write(self):
        for field in ("run_enabled", "formal_results_enabled"):
            with self.subTest(field=field):
                cfg = copy.deepcopy(self.config)
                cfg[field] = True
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    check_control(cfg)

    def test_shared_measurement_contract_cannot_drift(self):
        cfg = copy.deepcopy(self.config)
        cfg["measurement"]["time_metric"] = "median_ms"
        with self.assertRaisesRegex(ValueError, "measurement contract"):
            check_control(cfg)

    def test_one_tier_per_task_and_full_call_budget_are_required(self):
        cfg = copy.deepcopy(self.config)
        item = {"task_id": "EffiBench/22", "tier": "t3",
                "args_hash": "sha256:73cb9b3585c6c98b"}
        cfg["task_bank"]["frozen_task_tiers"] = [item, dict(item)]
        with self.assertRaisesRegex(ValueError, "exactly one frozen tier"):
            check_control(cfg)
        cfg["task_bank"]["frozen_task_tiers"] = [item]
        cfg["model"].update({"max_total_calls": 1, "max_total_tokens": 1000,
                             "max_total_cost_usd": 1.0})
        self.assertIn("call_budget_below_planned_generations",
                      check_control(cfg)["blockers"])


if __name__ == "__main__":
    unittest.main()
