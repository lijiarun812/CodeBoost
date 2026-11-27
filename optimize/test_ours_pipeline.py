"""Small in-memory loop tests with a mock candidate and replaceable harness."""

from __future__ import annotations

import copy
import hashlib
import unittest

from optimize.feedback import build_feedback
from optimize.ours_pipeline import run_offline_loop


def _task():
    frozen = {"tier": "t2", "n": 100, "gen": "seed:1",
              "args_hash": "fixed-hash", "test": "assert candidate(1) == 1"}
    task = {
        "task_id": "HumanEval/40", "dataset": "humaneval", "kind": "humaneval",
        "entry_point": "candidate", "description": "Complete problem statement",
        "code": "def candidate(x): return x",
        "test": "assert candidate(1) == 1", "inputs": [frozen],
    }
    return task, frozen


def _measured(task, plan, *, status="ok", time_ms=100.0, cv=0.01,
              heap_mb=10.0, rss_mb=100.0, args_hash=None):
    return {
        "task_id": task["task_id"], "status": status,
        "time": {"mean_trimmed_ms": time_ms, "cv": cv,
                 "tiers": [{"tier": plan["tier"],
                            "args_hash": plan["args_hash"] if args_hash is None else args_hash,
                            "status": status}]},
        "mem": {"peak_tracemalloc_mb": heap_mb, "peak_rss_mb": rss_mb},
        "downgraded": False,
    }


class FakeHarness:
    def __init__(self, cases):
        self.cases = cases
        self.calls = []

    def __call__(self, task, code=None, method="base", tier_plan=None, **kwargs):
        self.calls.append({"code": code, "method": method,
                           "tier_plan": copy.deepcopy(tier_plan), "kwargs": kwargs})
        plan = tier_plan[0]
        settings = self.cases["base" if code is None else code]
        return _measured(task, plan, **settings)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.task, self.frozen = _task()

    def run_case(self, mode, cases, *, feedback_method="f1", feedback_builder=None,
                 generator=None, rounds=1):
        harness = FakeHarness(cases)
        kw = {"feedback_builder": feedback_builder} if feedback_builder else {}
        output = run_offline_loop(
            self.task, self.frozen, mode=mode, feedback_method=feedback_method,
            generate_candidate=generator or (lambda prompt, code, index: "candidate-v1"),
            measure_task_fn=harness, rounds=rounds, **kw,
        )
        return output, harness

    def test_same_frozen_tier_hash_and_initial_code(self):
        task_before = copy.deepcopy(self.task)
        output, harness = self.run_case(
            "time-only", {"base": {}, "candidate-v1": {"time_ms": 80}}
        )
        self.assertEqual(output["rounds"][0]["decision"], "accept")
        self.assertEqual(output["rounds"][0]["reason"], "accepted_time_gain")
        self.assertEqual(output["initial_code"], task_before["code"])
        self.assertEqual(output["current_code"], "candidate-v1")
        self.assertIsNone(harness.calls[0]["code"])
        self.assertEqual([call["tier_plan"] for call in harness.calls],
                         [[self.frozen], [self.frozen]])
        self.assertEqual(harness.calls[0]["method"], "base")
        self.assertEqual(harness.calls[1]["method"], "f1")
        self.assertEqual(self.task, task_before)
        self.assertFalse(output["formal_result_written"])

    def test_pair_mismatch_is_rejected(self):
        output, _ = self.run_case(
            "joint", {"base": {}, "candidate-v1": {
                "time_ms": 80, "heap_mb": 8, "args_hash": "wrong",
            }}
        )
        self.assertEqual((output["rounds"][0]["decision"], output["rounds"][0]["reason"]),
                         ("reject", "pair_mismatch"))
        self.assertEqual(output["current_code"], self.task["code"])

    def test_incorrect_candidate_is_rejected(self):
        output, _ = self.run_case(
            "time-only", {"base": {}, "candidate-v1": {"status": "incorrect"}}
        )
        self.assertEqual(output["rounds"][0]["reason"], "candidate_status_incorrect")
        self.assertEqual(output["current_code"], self.task["code"])

    def test_missing_memory_retests_space_modes(self):
        for mode in ("mem-only", "joint"):
            with self.subTest(mode=mode):
                output, _ = self.run_case(
                    mode, {"base": {}, "candidate-v1": {
                        "time_ms": 80, "heap_mb": None,
                    }}
                )
                self.assertEqual((output["rounds"][0]["decision"],
                                  output["rounds"][0]["reason"]),
                                 ("retest", "memory_unmeasurable"))
                self.assertEqual(output["current_code"], self.task["code"])

    def test_first_round_retest_stops_and_preserves_candidate_measurement(self):
        generated = []
        def generator(prompt, code, index):
            generated.append((code, index))
            return f"candidate-v{index + 1}"
        output, harness = self.run_case(
            "joint", {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": None}},
            generator=generator, rounds=2,
        )
        self.assertEqual(generated, [(self.task["code"], 0)])
        self.assertEqual(len(harness.calls), 2)  # no automatic retest
        self.assertEqual(len(output["rounds"]), 1)
        self.assertEqual(output["stop_reason"], "pending_retest")
        self.assertEqual(output["current_code"], self.task["code"])
        pending = output["pending_retest"]
        self.assertIs(pending, output["rounds"][0])
        self.assertEqual((pending["decision"], pending["reason"]),
                         ("retest", "memory_unmeasurable"))
        self.assertEqual(pending["candidate_code"], "candidate-v1")
        self.assertIsNone(pending["candidate_measurement"]["mem"]["peak_tracemalloc_mb"])
        self.assertEqual(pending["baseline_measurement"]["time"]["mean_trimmed_ms"], 100)
        self.assertEqual(output["kind"], "diagnostic_demo")
        self.assertFalse(output["formal_result_written"])
        self.assertFalse(pending["formal_result_written"])

    def test_retest_after_accept_keeps_last_accepted_code(self):
        generated = []
        def generator(prompt, code, index):
            generated.append((code, index))
            return f"candidate-v{index + 1}"
        output, harness = self.run_case(
            "joint", {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 9},
                      "candidate-v2": {"time_ms": 70, "heap_mb": None}},
            generator=generator, rounds=3,
        )
        self.assertEqual([row["decision"] for row in output["rounds"]],
                         ["accept", "retest"])
        self.assertEqual(generated, [(self.task["code"], 0), ("candidate-v1", 1)])
        self.assertEqual(len(harness.calls), 3)
        self.assertEqual(output["current_code"], "candidate-v1")
        self.assertEqual(output["stop_reason"], "pending_retest")
        self.assertEqual(output["pending_retest"]["candidate_code"], "candidate-v2")
        self.assertEqual(output["pending_retest"]["baseline_measurement"]["mem"]
                         ["peak_tracemalloc_mb"], 9)

    def test_three_modes_keep_distinct_acceptance_reasons(self):
        cases = {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 12}}
        time_only, _ = self.run_case("time-only", cases)
        mem_only, _ = self.run_case("mem-only", cases)
        joint, _ = self.run_case("joint", cases)
        self.assertEqual(time_only["rounds"][0]["reason"],
                         "accepted_time_gain_with_memory_regression")
        self.assertEqual(mem_only["rounds"][0]["reason"], "memory_gain_below_threshold")
        self.assertEqual(joint["rounds"][0]["reason"], "memory_regression_too_large")

    def test_each_round_records_reason_and_updates_current_only_on_accept(self):
        cases = {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 10.5},
                 "candidate-v2": {"time_ms": 70, "heap_mb": 11}}
        seen = []
        def generator(prompt, code, index):
            seen.append((index, code, prompt))
            return f"candidate-v{index + 1}"
        output, harness = self.run_case("joint", cases, generator=generator, rounds=2)
        self.assertEqual([row["reason"] for row in output["rounds"]],
                         ["accepted_time_gain_with_memory_regression"] * 2)
        self.assertEqual(seen[0][1], self.task["code"])
        self.assertEqual(seen[1][1], "candidate-v1")
        self.assertIn(self.task["description"], seen[0][2])
        self.assertEqual(output["current_code"], "candidate-v2")
        self.assertEqual(len(harness.calls), 3)

    def test_incomplete_f4_stays_diagnostic_even_if_candidate_accepted(self):
        def unfinished_feedback(method, task, code, total_ms=None):
            return {"prompt": "offline prompt", "method": None,
                    "requested_method": "ours", "effective_method": "f4_line_hits",
                    "degrade_reason": None, "can_write_result": False}
        output, harness = self.run_case(
            "time-only", {"base": {}, "candidate-v1": {"time_ms": 80}},
            feedback_method="f4", feedback_builder=unfinished_feedback,
        )
        self.assertEqual(output["kind"], "diagnostic_demo")
        self.assertEqual(output["rounds"][0]["decision"], "accept")
        self.assertFalse(output["rounds"][0]["feedback"]["can_write_result"])
        self.assertFalse(output["rounds"][0]["formal_result_written"])
        self.assertEqual(harness.calls[1]["method"], "diagnostic")

    def test_real_c_f4_projection_keeps_full_and_hits_only_out_of_results(self):
        diagnostic = {"status": "ok", "mode": "humaneval", "cases": [
            {"idx": 0, "ms": 2.5, "src": "assert candidate(1) == 1"}],
            "slowest": {"idx": 0, "ms": 2.5, "src": "assert candidate(1) == 1"}}
        timing = {"ok": True, "status": "ok", "mode": "line_profiler", "unit": "ms",
                  "source_sha256": hashlib.sha256(self.task["code"].encode()).hexdigest(),
                  "items": [{"line": 1, "hits": 1, "line_time_ms": 3.0,
                             "src": self.task["code"]}]}
        for variant in ("full", "hits_only"):
            with self.subTest(variant=variant):
                case = diagnostic if variant == "full" else {
                    **diagnostic, "slowest": {**diagnostic["slowest"], "ms": 0.3}}
                def c_feedback(method, task, code, total_ms=None):
                    return build_feedback(method, task, code, total_ms=total_ms,
                                          diagnostic=case, line_timing=timing,
                                          hot={"ok": True, "items": [{"line": 1, "hits": 4}]})
                output, harness = self.run_case(
                    "time-only", {"base": {}, "candidate-v1": {"time_ms": 80}},
                    feedback_method="f4", feedback_builder=c_feedback,
                )
                feedback = output["rounds"][0]["feedback"]
                self.assertEqual(feedback["requested_method"], "ours")
                self.assertEqual(feedback["effective_method"],
                                 "f4" if variant == "full" else "f4_line_hits")
                self.assertIsNone(feedback["method"])
                self.assertTrue(feedback["schema_mapping_pending"])
                self.assertFalse(feedback["eligible_for_requested_method"])
                self.assertFalse(feedback["can_write_result"])
                self.assertIsInstance(feedback["diagnostics"], dict)
                self.assertEqual(harness.calls[1]["method"], "diagnostic")
                self.assertEqual(output["kind"], "diagnostic_demo")
                self.assertFalse(output["formal_result_written"])

    def test_real_c_f4_fallback_uses_actual_schema_method(self):
        diagnostic = {"status": "ok", "slowest": {
            "idx": 0, "ms": 2.5, "src": "assert candidate(1) == 1"}}
        def c_feedback(method, task, code, total_ms=None):
            return build_feedback(method, task, code, total_ms=total_ms,
                                  diagnostic=diagnostic,
                                  line_timing={"ok": False, "status": "timeout",
                                               "reason": "diagnostic timeout"},
                                  hot={"ok": False, "reason": "hotspot timeout"})
        output, harness = self.run_case(
            "time-only", {"base": {}, "candidate-v1": {"time_ms": 80}},
            feedback_method="f4", feedback_builder=c_feedback,
        )
        feedback = output["rounds"][0]["feedback"]
        self.assertEqual((feedback["method"], feedback["requested_method"],
                          feedback["effective_method"], feedback["degrade_reason"]),
                         ("f2", "ours", "f2", "fallback_f2"))
        self.assertTrue(feedback["can_write_result"])
        self.assertFalse(feedback["eligible_for_requested_method"])
        self.assertEqual(harness.calls[1]["method"], "f2")
        self.assertFalse(output["formal_result_written"])

    def test_writable_feedback_without_schema_method_is_rejected(self):
        harness = FakeHarness({"base": {}})
        with self.assertRaisesRegex(ValueError, "schema method"):
            run_offline_loop(
                self.task, self.frozen, mode="time-only", feedback_method="f1",
                generate_candidate=lambda *args: self.fail("generator should not run"),
                measure_task_fn=harness,
                feedback_builder=lambda *args, **kwargs: {
                    "prompt": "prompt", "method": None, "requested_method": "f1",
                    "effective_method": "f1", "degrade_reason": None,
                    "can_write_result": True,
                },
            )

    def test_frozen_task_validation_and_failed_baseline(self):
        harness = FakeHarness({"base": {"status": "timeout"}})
        output = run_offline_loop(
            self.task, self.frozen, mode="joint", feedback_method="f1",
            generate_candidate=lambda *args: self.fail("generator should not run"),
            measure_task_fn=harness,
        )
        self.assertEqual(output["stop_reason"], "baseline_status_timeout")
        self.assertEqual(output["rounds"], [])
        with self.assertRaisesRegex(ValueError, "Task.inputs"):
            run_offline_loop(
                self.task, {**self.frozen, "args_hash": "other"},
                mode="joint", feedback_method="f1",
                generate_candidate=lambda *args: "candidate-v1",
                measure_task_fn=harness,
            )
        task = copy.deepcopy(self.task)
        task["inputs"][0]["args_hash"] = None
        with self.assertRaisesRegex(ValueError, "nonempty args_hash"):
            run_offline_loop(
                task, task["inputs"][0], mode="joint", feedback_method="f1",
                generate_candidate=lambda *args: "candidate-v1",
                measure_task_fn=harness,
            )

    def test_real_harness_result_shape_smoke(self):
        """A tiny local smoke check of the actual measure_task return shape."""
        frozen = {"tier": "t1", "n": 20, "gen": "offline",
                  "args_hash": "local-fixed-input", "test": "assert solution(20) == 190"}
        task = {"task_id": "local/sum", "dataset": "humaneval", "kind": "assert",
                "entry_point": "solution", "description": "Sum integers below n.",
                "code": "def solution(n):\n    return sum(range(n))\n",
                "test": frozen["test"], "inputs": [frozen]}
        output = run_offline_loop(
            task, frozen, mode="time-only", feedback_method="f1",
            generate_candidate=lambda prompt, code, index:
                "def solution(n):\n    return n * (n - 1) // 2\n",
            measurement_kwargs={"e_reps": 2, "target_batch_s": 0.01, "max_reps": 200},
        )
        self.assertEqual(output["kind"], "diagnostic_demo")
        self.assertFalse(output["formal_result_written"])
        row = output["rounds"][0]
        self.assertEqual(row["candidate_status"], "ok")
        self.assertIn(row["decision"], ("accept", "reject", "retest"))
        self.assertEqual(row["decision_detail"]["raw"]["baseline"]["args_hash"],
                         frozen["args_hash"])
        self.assertEqual(row["decision_detail"]["raw"]["candidate"]["tier"], "t1")


if __name__ == "__main__":
    unittest.main()
