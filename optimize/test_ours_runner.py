"""Offline runner tests; all writer effects are confined to temporary dirs."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from optimize.feedback import build_feedback
from optimize.ours_runner import run_offline_ours
from optimize.test_ours_pipeline import FakeHarness, _task


class OfflineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.task, self.frozen = _task()
        self.rss_record = {"environment_id": "offline-env", "peak_rss_mb": 95.0,
                           "how": "injected local idle-process measurement"}

    def run_case(self, cases, *, mode="joint", feedback_method="f1", **kwargs):
        harness = FakeHarness(cases)
        output = run_offline_ours(
            self.task, self.frozen, mode=mode, feedback_method=feedback_method,
            generate_candidate=lambda prompt, code, index: "candidate-v1",
            model="mock-model", model_version="offline-v1",
            prompt_version="feedback-v1", environment_id="offline-env",
            rss_baseline_record=self.rss_record,
            measure_task_fn=harness, **kwargs,
        )
        return output, harness

    def test_joint_records_frozen_input_raw_measurements_and_metadata(self):
        output, harness = self.run_case(
            {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}},
        )
        row = output["rounds"][0]
        self.assertEqual(row["decision"], "accept")
        self.assertEqual(row["source_code"], self.task["code"])
        self.assertEqual(row["candidate_code"], "candidate-v1")
        self.assertIn(self.task["description"], row["prompt"])
        self.assertEqual(row["baseline_measurement"]["time"]["mean_trimmed_ms"], 100)
        self.assertEqual(row["candidate_measurement"]["mem"]["peak_tracemalloc_mb"], 8)
        self.assertEqual(row["feedback"]["requested_method"], "f1")
        self.assertEqual(row["feedback"]["method"], "f1")
        self.assertEqual((row["model"], row["prompt_version"]),
                         ("mock-model", "feedback-v1"))
        self.assertEqual(output["rss_baseline_record"], self.rss_record)
        self.assertEqual(output["rss_baseline_mb"], 95.0)
        self.assertTrue(all(call["tier_plan"] == [self.frozen] for call in harness.calls))
        self.assertTrue(all(call["kwargs"]["model"] == "mock-model" for call in harness.calls))
        self.assertFalse(output["formal_result_written"])

    def test_joint_requires_current_environment_rss_record(self):
        harness = FakeHarness({"base": {}})
        common = dict(task=self.task, frozen_input=self.frozen, mode="joint",
                      feedback_method="f1", generate_candidate=lambda *args: "candidate-v1",
                      model="mock", model_version="v1", prompt_version="p1",
                      environment_id="offline-env", measure_task_fn=harness)
        for record in (None, {"environment_id": "other", "peak_rss_mb": 95.0},
                       {"environment_id": "offline-env", "peak_rss_mb": None}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                run_offline_ours(**common, rss_baseline_record=record)
        self.assertEqual(harness.calls, [])

    def test_joint_passes_recorded_rss_baseline_into_a_gate(self):
        output, _ = self.run_case(
            {"base": {}, "candidate-v1": {
                "time_ms": 80, "heap_mb": 8, "rss_mb": 130}},
        )
        row = output["rounds"][0]
        self.assertEqual(row["decision"], "reject")
        self.assertTrue(row["decision_detail"]["gate_failed"])
        self.assertEqual(row["decision_detail"]["rss_status"], "gated")
        self.assertEqual(output["current_code"], self.task["code"])

    def test_full_f4_writes_diagnostic_only_even_with_result_writer(self):
        diagnostic = {"status": "ok", "slowest": {
            "idx": 0, "ms": 2.5, "src": "assert candidate(1) == 1"}}
        timing = {"ok": True, "status": "ok", "mode": "line_profiler", "unit": "ms",
                  "source_sha256": hashlib.sha256(self.task["code"].encode()).hexdigest(),
                  "items": [{"line": 1, "hits": 1, "line_time_ms": 3.0,
                             "src": self.task["code"]}]}
        def c_feedback(method, task, code, total_ms=None):
            return build_feedback(method, task, code, total_ms=total_ms,
                                  diagnostic=diagnostic, line_timing=timing)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_path, diagnostic_path = root / "result.json", root / "diagnostic.json"
            output, _ = self.run_case(
                {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}},
                feedback_method="f4", feedback_builder=c_feedback,
                allow_formal_results=True,
                write_result=lambda record: formal_path.write_text(json.dumps(record)),
                write_diagnostic=lambda record: diagnostic_path.write_text(json.dumps(record)),
            )
            self.assertTrue(diagnostic_path.exists())
            self.assertFalse(formal_path.exists())
            self.assertEqual(output["formal_results_written"], 0)
            self.assertFalse(output["formal_result_written"])
            self.assertFalse(output["rounds"][0]["feedback"]["can_write_result"])
            self.assertEqual(json.loads(diagnostic_path.read_text())["kind"],
                             "diagnostic_demo")

    def test_opt_in_result_writer_uses_schema_method_only_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_path = root / "result.json"
            output, _ = self.run_case(
                {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}},
                allow_formal_results=True,
                write_result=lambda record: formal_path.write_text(json.dumps(record)),
            )
            self.assertTrue(formal_path.exists())
            stored = json.loads(formal_path.read_text())
            self.assertEqual((stored["method"], stored["requested_method"],
                              stored["degrade_reason"]), ("f1", "f1", None))
            self.assertEqual(output["formal_results_written"], 1)


if __name__ == "__main__":
    unittest.main()
