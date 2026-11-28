"""Offline C/D gate integration checks; no model or formal Result writer runs."""

from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from eval.gate import check
from optimize.feedback import build_feedback
from optimize.ours_runner import run_offline_ours
from optimize.test_ours_pipeline import FakeHarness, _task


class F4GateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.task, self.frozen = _task()
        self.hot = {"ok": True, "items": [{"line": 1, "hits": 4}]}
        self.timing = {
            "ok": True, "status": "ok", "mode": "line_profiler", "unit": "ms",
            "source_sha256": hashlib.sha256(self.task["code"].encode()).hexdigest(),
            "items": [{"line": 1, "hits": 4, "line_time_ms": 3.0,
                       "src": self.task["code"]}],
            "unprofiled_case_wall_ms": 2.5,
            "profiled_case_wall_ms": 6.0,
            "profiler_overhead_ratio": 999.0,  # ignored; derive from observed walls
            "overhead_method": "same_child_fresh_namespace_case_wall",
        }

    def builder(self, slow_ms=2.5, timing=None):
        diagnostic = {"status": "ok", "slowest": {
            "idx": 0, "ms": slow_ms, "src": "assert candidate(1) == 1"}}

        def make(method, task, code, **kwargs):
            return build_feedback(method, task, code, diagnostic=diagnostic,
                                  hot=self.hot, line_timing=timing if timing is not None else self.timing,
                                  **kwargs)
        return make

    def run_case(self, builder, *, heap_mb=10.0, candidate_heap_mb=8.0):
        harness = FakeHarness({"base": {"heap_mb": heap_mb},
                               "candidate-v1": {"time_ms": 80,
                                                "heap_mb": candidate_heap_mb}})
        writes = []
        output = run_offline_ours(
            self.task, self.frozen, mode="joint", feedback_method="f4",
            generate_candidate=lambda *args: "candidate-v1",
            model="mock", model_version="offline", prompt_version="gate-v1",
            environment_id="offline-env", measure_task_fn=harness,
            feedback_builder=builder,
            ensure_rss_baseline_fn=lambda: {"status": "ok", "value": 95.0,
                                            "reason": "mock", "meta": {}},
            write_result=writes.append, allow_formal_results=False,
        )
        self.assertEqual(writes, [])
        self.assertFalse(output["formal_result_written"])
        return output, harness

    def test_complete_f4_passes_gate_without_writing_result(self):
        output, harness = self.run_case(self.builder())
        row = output["rounds"][0]
        self.assertEqual(row["decision"], "accept")
        self.assertTrue(row["gate"]["passed"], row["gate"]["failures"])
        self.assertTrue(row["gate"]["c_feedback_complete"])
        self.assertEqual(row["feedback"]["method"], "ours")
        self.assertTrue(row["feedback"]["can_write_result"])
        self.assertEqual(row["feedback"]["diagnostics"]["profiler_overhead_ratio"], 2.4)
        self.assertNotEqual(row["prompt_hashes"]["f3"], row["prompt_hashes"]["f4"])
        self.assertIn("Case execution time: 2.5000 ms", row["feedback_text"])
        self.assertIn("Hot lines (占已剖析行时间之和", row["feedback_text"])
        self.assertNotIn("ms", row["feedback_text"].split("Hot lines", 1)[1])
        self.assertEqual(harness.calls[1]["method"], "diagnostic")

    def test_below_floor_space_uses_na_and_can_pass_gate(self):
        output, _ = self.run_case(self.builder(), heap_mb=0.001,
                                  candidate_heap_mb=0.001)
        row = output["rounds"][0]
        self.assertEqual(row["feedback"]["diagnostics"]["space_status"], "below_floor")
        self.assertIn("n/a（本题空间不可分辨", row["feedback_text"])
        self.assertTrue(row["gate"]["passed"], row["gate"]["failures"])
        self.assertEqual(row["feedback"]["method"], "ours")

    def test_missing_program_peak_is_explicit_na(self):
        output, _ = self.run_case(self.builder(), heap_mb=None,
                                  candidate_heap_mb=8.0)
        row = output["rounds"][0]
        self.assertEqual(row["feedback"]["diagnostics"]["space_status"], "unmeasured")
        self.assertIn("peak_tracemalloc_mb：n/a", row["feedback_text"])
        self.assertTrue(row["gate"]["passed"], row["gate"]["failures"])
        self.assertFalse(output["formal_result_written"])

    def test_small_case_is_f3_not_applicable_and_never_ours(self):
        output, _ = self.run_case(self.builder(slow_ms=0.3))
        row = output["rounds"][0]
        diag = row["feedback"]["diagnostics"]
        self.assertEqual((row["feedback"]["method"], row["feedback"]["degrade_reason"]),
                         ("f3", "no_hotspot"))
        self.assertEqual(diag["not_applicable"]["profiler_overhead_ratio"],
                         "case_too_small")
        self.assertEqual(diag["line_timing"]["numbers_in"], "hits_only")
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertNotIn("ms", row["feedback_text"].split("Hot lines", 1)[1])
        self.assertNotIn("%", row["feedback_text"].split("Hot lines", 1)[1])

    def test_missing_profiler_dependency_stays_diagnostic(self):
        missing = {"ok": False, "status": "missing_dependency",
                   "reason": "line_profiler unavailable"}
        output, _ = self.run_case(self.builder(timing=missing))
        row = output["rounds"][0]
        self.assertEqual(row["feedback"]["effective_method"], "f4_line_hits")
        self.assertFalse(row["gate"]["passed"])
        self.assertIn("G1", {item["code"] for item in row["gate"]["failures"]})
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertIsNone(row["feedback"]["method"])
        self.assertEqual(row["suggested_rerun_method"], "f3")

    def test_incorrect_candidate_cannot_become_writable_ours(self):
        harness = FakeHarness({"base": {}, "candidate-v1": {"status": "incorrect"}})
        writes = []
        output = run_offline_ours(
            self.task, self.frozen, mode="joint", feedback_method="f4",
            generate_candidate=lambda *args: "candidate-v1",
            model="mock", model_version="offline", prompt_version="gate-v1",
            environment_id="offline-env", measure_task_fn=harness,
            feedback_builder=self.builder(),
            ensure_rss_baseline_fn=lambda: {"status": "ok", "value": 95.0,
                                            "reason": "mock", "meta": {}},
            write_result=writes.append, allow_formal_results=False,
        )
        row = output["rounds"][0]
        self.assertEqual(row["decision"], "reject")
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertIsNone(row["feedback"]["method"])
        self.assertEqual(writes, [])

    def test_identical_f3_f4_prompt_is_blocked_by_g5(self):
        original = self.builder()

        def same_prompt(method, task, code, **kwargs):
            f3 = original("f3", task, code, **kwargs)
            if method == "f4":
                f3.update({"requested_method": "ours", "effective_method": "f4",
                           "method": None, "can_write_result": False})
                f3["diagnostics"].update({
                    "hotspot_mode": "line_profiler", "case_ms": 2.5,
                    "profiler_overhead_ratio": 2.4, "space_status": "measured",
                    "feedback_complete": True,
                    "line_timing": {"numbers_in": "hits_only"},
                })
            return f3

        output, _ = self.run_case(same_prompt)
        row = output["rounds"][0]
        self.assertEqual(row["prompt_hashes"]["f3"], row["prompt_hashes"]["f4"])
        self.assertFalse(row["gate"]["passed"])
        self.assertIn("G5", {item["code"] for item in row["gate"]["failures"]})
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertIsNone(row["feedback"]["method"])

    def test_complete_flag_alone_cannot_open_ours(self):
        original = self.builder()

        def false_complete(method, task, code, **kwargs):
            result = original(method, task, code, **kwargs)
            if method == "f4":
                result["diagnostics"]["feedback_complete"] = False
            return result

        output, _ = self.run_case(false_complete)
        row = output["rounds"][0]
        self.assertTrue(row["gate"]["passed"])
        self.assertFalse(row["gate"]["c_feedback_complete"])
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertIsNone(row["feedback"]["method"])

    def test_unavailable_f3_comparison_hash_blocks_ours(self):
        original = self.builder()

        def no_f3(method, task, code, **kwargs):
            if method == "f3":
                raise RuntimeError("offline F3 comparison unavailable")
            return original(method, task, code, **kwargs)

        output, _ = self.run_case(no_f3)
        row = output["rounds"][0]
        self.assertIsNone(row["prompt_hashes"]["f3"])
        self.assertFalse(row["gate"]["passed"])
        self.assertIn("G5", {item["code"] for item in row["gate"]["failures"]})
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertIsNone(row["feedback"]["method"])

    def test_status_mismatch_is_blocked_by_g4(self):
        output, _ = self.run_case(self.builder())
        row = output["rounds"][0]
        report = check(
            diagnostics=row["feedback"]["diagnostics"],
            feedback=row["feedback_text"], result_status="incorrect",
            harness_status=row["candidate_measurement"]["status"],
            prompt_hash_f3=row["prompt_hashes"]["f3"],
            prompt_hash_f4=row["prompt_hashes"]["f4"], method="ours",
        )
        self.assertFalse(report["passed"])
        self.assertIn("G4", {item["code"] for item in report["failures"]})

    def test_runner_rechecks_harness_status_snapshot_before_formal_projection(self):
        output, _ = self.run_case(self.builder())
        row = output["rounds"][0]
        row["candidate_measurement"]["status"] = "incorrect"
        self.assertEqual(row["candidate_status"], "ok")
        writes = []
        with patch("optimize.ours_runner.run_offline_loop", return_value=output):
            replay = run_offline_ours(
                self.task, self.frozen, mode="joint", feedback_method="f4",
                generate_candidate=lambda *args: self.fail("candidate must not be regenerated"),
                model="mock", model_version="offline", prompt_version="gate-v1",
                environment_id="offline-env",
                ensure_rss_baseline_fn=lambda: {"status": "ok", "value": 95.0,
                                                "reason": "mock", "meta": {}},
                write_result=writes.append, allow_formal_results=False,
            )
        self.assertIn("G4", {item["code"] for item in replay["rounds"][0]["gate"]["failures"]})
        self.assertFalse(replay["rounds"][0]["feedback"]["can_write_result"])
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
