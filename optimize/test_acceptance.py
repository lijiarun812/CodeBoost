"""Constructed Result data only: no model, harness run, files, or API."""

from __future__ import annotations

import copy
import unittest

from optimize.acceptance import AcceptanceConfig, decide_acceptance


def result(
    *,
    task_id="HumanEval/40", tier="t2", args_hash="abc123",
    status="ok", tier_status="ok", time_ms=100.0, cv=0.01,
    heap_mb=10.0, rss_mb=100.0,
):
    return {
        "task_id": task_id, "status": status,
        "time": {
            "mean_trimmed_ms": time_ms, "cv": cv,
            "tiers": [{"tier": tier, "args_hash": args_hash, "status": tier_status}],
        },
        "mem": {"peak_tracemalloc_mb": heap_mb, "peak_rss_mb": rss_mb},
        "downgraded": False,
    }


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.base = result()

    def decide(self, mode, **candidate_kwargs):
        return decide_acceptance(self.base, result(**candidate_kwargs), mode)

    def test_time_only_accepts_time_gain_and_records_memory_regression(self):
        answer = self.decide("time-only", time_ms=80, heap_mb=12)
        self.assertEqual(answer["decision"], "accept")
        self.assertEqual(answer["reason"], "accepted_time_gain_with_memory_regression")
        self.assertAlmostEqual(answer["derived"]["time_gain_fraction"], 0.2)
        self.assertAlmostEqual(answer["derived"]["memory_gain_fraction"], -0.2)
        self.assertEqual(answer["raw"]["candidate"]["peak_rss_mb"], 100.0)
        self.assertFalse(answer["derived"]["pareto_dominates"])

    def test_mem_only_accepts_heap_gain_despite_time_regression(self):
        answer = self.decide("mem-only", time_ms=130, heap_mb=8)
        self.assertEqual(answer["decision"], "accept")
        self.assertEqual(answer["reason"], "accepted_memory_gain_with_time_regression")
        self.assertAlmostEqual(answer["derived"]["memory_gain_fraction"], 0.2)

    def test_joint_distinguishes_pareto_from_bounded_tradeoff(self):
        pareto = self.decide("joint", time_ms=80, heap_mb=9)
        self.assertEqual((pareto["decision"], pareto["reason"], pareto["relationship"]),
                         ("accept", "accepted_pareto_dominance", "pareto_dominates"))
        self.assertTrue(pareto["derived"]["pareto_dominates"])
        tradeoff = self.decide("joint", time_ms=80, heap_mb=10.5)
        self.assertEqual((tradeoff["decision"], tradeoff["reason"], tradeoff["relationship"]),
                         ("accept", "accepted_time_gain_with_memory_regression",
                          "bounded_tradeoff"))
        rejected = self.decide("joint", time_ms=80, heap_mb=11.5)
        self.assertEqual((rejected["decision"], rejected["reason"]),
                         ("reject", "memory_regression_too_large"))

    def test_joint_rejects_candidate_dominated_on_both_metrics(self):
        answer = self.decide("joint", time_ms=120, heap_mb=12)
        self.assertEqual((answer["decision"], answer["relationship"]),
                         ("reject", "baseline_dominates"))

    def test_status_failure_rejects_before_pairing(self):
        for status in ("incorrect", "timeout", "resource_exceeded", "api_failed"):
            with self.subTest(status=status):
                answer = self.decide("joint", status=status, args_hash="different")
                self.assertEqual(answer["decision"], "reject")
                self.assertEqual(answer["reason"], f"candidate_status_{status}")
        baseline = result(status="timeout")
        answer = decide_acceptance(baseline, result(), "time-only")
        self.assertEqual(answer["reason"], "baseline_status_timeout")

    def test_task_tier_and_hash_must_match(self):
        for changes in ({"task_id": "HumanEval/55"}, {"tier": "t3"},
                        {"args_hash": "other"}):
            with self.subTest(changes=changes):
                answer = self.decide("joint", **changes)
                self.assertEqual((answer["decision"], answer["reason"]),
                                 ("reject", "pair_mismatch"))
        for changes in ({"args_hash": None}, {"tier": ""}, {"tier_status": "timeout"}):
            with self.subTest(changes=changes):
                answer = self.decide("joint", **changes)
                self.assertEqual(answer["reason"], "pair_unavailable")

    def test_last_attempted_tier_is_the_harness_selected_tier(self):
        baseline = result()
        baseline["time"]["tiers"].insert(
            0, {"tier": "t1", "args_hash": "earlier", "status": "ok"}
        )
        matched = decide_acceptance(baseline, result(time_ms=80), "time-only")
        self.assertEqual(matched["decision"], "accept")
        self.assertEqual(matched["raw"]["baseline"]["tier"], "t2")
        self.assertEqual(matched["raw"]["baseline"]["args_hash"], "abc123")
        baseline["time"]["tiers"].append(
            {"tier": "t3", "args_hash": "failed", "status": "timeout"}
        )
        self.assertEqual(decide_acceptance(baseline, result(), "time-only")["reason"],
                         "pair_unavailable")

    def test_missing_zero_and_tiny_heap_never_form_memory_ratio(self):
        for heap in (None, 0.0, 0.001):
            with self.subTest(heap=heap):
                for mode in ("mem-only", "joint"):
                    answer = self.decide(mode, time_ms=80, heap_mb=heap)
                    self.assertEqual((answer["decision"], answer["reason"]),
                                     ("retest", "memory_unmeasurable"))
                    self.assertIsNone(answer["derived"]["memory_gain_fraction"])
        time_only = self.decide("time-only", time_ms=80, heap_mb=0.0)
        self.assertEqual(time_only["decision"], "accept")
        self.assertIsNone(time_only["derived"]["memory_gain_fraction"])

    def test_baseline_heap_zero_is_unmeasurable(self):
        base = result(heap_mb=0)
        answer = decide_acceptance(base, result(heap_mb=8), "joint")
        self.assertEqual(answer["reason"], "memory_unmeasurable")
        self.assertIsNone(answer["derived"]["memory_gain_fraction"])

    def test_noise_boundary_and_effective_time_floor(self):
        high = self.decide("time-only", time_ms=60, cv=0.1001)
        self.assertEqual((high["decision"], high["reason"]),
                         ("retest", "time_noise_high"))
        boundary = self.decide("time-only", time_ms=70, cv=0.10)
        self.assertEqual(boundary["decision"], "accept")
        self.assertAlmostEqual(boundary["derived"]["effective_time_threshold"], 0.20)

    def test_mem_only_does_not_gate_on_time_cv(self):
        answer = self.decide("mem-only", time_ms=130, cv=0.25, heap_mb=8)
        self.assertEqual(answer["decision"], "accept")
        self.assertEqual(answer["reason"], "accepted_memory_gain_time_uncertain")
        self.assertFalse(answer["derived"]["time_reliable"])
        self.assertIsNone(answer["derived"]["pareto_dominates"])
        self.assertEqual(self.decide("joint", time_ms=80, cv=0.25, heap_mb=8)["reason"],
                         "time_noise_high")
        no_time = self.decide("mem-only", time_ms=0, cv=None, heap_mb=8)
        self.assertEqual(no_time["decision"], "accept")
        self.assertIsNone(no_time["derived"]["time_gain_fraction"])

    def test_status_ok_with_failed_memory_pass_is_not_space_evidence(self):
        # harness.measure_task can emit status=ok with a null heap peak when
        # its separate memory pass failed; time-only can still use clean time.
        self.assertEqual(self.decide("mem-only", time_ms=80, heap_mb=None)["reason"],
                         "memory_unmeasurable")
        self.assertEqual(self.decide("joint", time_ms=80, heap_mb=None)["reason"],
                         "memory_unmeasurable")
        self.assertEqual(self.decide("time-only", time_ms=80, heap_mb=None)["decision"],
                         "accept")

    def test_gain_and_regression_threshold_boundaries(self):
        self.assertEqual(self.decide("time-only", time_ms=95)["reason"],
                         "time_near_threshold")
        self.assertEqual(self.decide("time-only", time_ms=92)["decision"], "accept")
        self.assertEqual(self.decide("time-only", time_ms=98)["decision"], "reject")
        self.assertEqual(self.decide("mem-only", heap_mb=9)["reason"],
                         "memory_near_threshold")
        self.assertEqual(self.decide("mem-only", heap_mb=8)["decision"], "accept")
        joint = self.decide("joint", time_ms=80, heap_mb=11)
        self.assertEqual((joint["decision"], joint["reason"]),
                         ("retest", "joint_near_threshold"))

    def test_rss_is_only_a_cap_and_report_value(self):
        config = AcceptanceConfig(max_rss_mb=200)
        capped = decide_acceptance(self.base, result(time_ms=80, rss_mb=201),
                                   "time-only", config)
        self.assertEqual((capped["decision"], capped["reason"]),
                         ("reject", "candidate_rss_limit_exceeded"))
        absent = decide_acceptance(self.base, result(time_ms=80, rss_mb=None),
                                   "time-only", config)
        self.assertEqual((absent["decision"], absent["reason"]),
                         ("retest", "candidate_rss_unavailable"))
        no_cap = decide_acceptance(self.base, result(time_ms=80, rss_mb=None),
                                   "time-only", AcceptanceConfig(max_rss_mb=None))
        self.assertEqual(no_cap["decision"], "accept")
        self.assertIsNone(no_cap["raw"]["candidate"]["peak_rss_mb"])
        lower_rss_only = decide_acceptance(self.base, result(rss_mb=20), "mem-only", config)
        self.assertEqual(lower_rss_only["decision"], "reject")
        self.assertEqual(lower_rss_only["raw"]["candidate"]["peak_rss_mb"], 20)

    def test_invalid_time_and_configurable_thresholds(self):
        answer = self.decide("time-only", time_ms=0)
        self.assertEqual(answer["reason"], "candidate_time_unmeasurable")
        config = AcceptanceConfig(min_time_gain=0.20, threshold_margin=0.005)
        strict = decide_acceptance(self.base, result(time_ms=85), "time-only", config)
        self.assertEqual(strict["decision"], "reject")
        self.assertEqual(strict["thresholds"]["min_time_gain"], 0.20)
        with self.assertRaises(ValueError):
            AcceptanceConfig(min_heap_mb=0)
        with self.assertRaises(ValueError):
            decide_acceptance(self.base, result(), "unknown")

    def test_inputs_are_not_mutated(self):
        base = result()
        cand = result(time_ms=80)
        old_base, old_cand = copy.deepcopy(base), copy.deepcopy(cand)
        decide_acceptance(base, cand, "joint")
        self.assertEqual(base, old_base)
        self.assertEqual(cand, old_cand)


if __name__ == "__main__":
    unittest.main()
