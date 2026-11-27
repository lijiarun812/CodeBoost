"""Offline behavior checks; no model client or paid API is used."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from optimize.feedback import build_feedback
from optimize.profiler import context_regions

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets" / "sample_humaneval_20.json"


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.code = "def solve(values):\n    for value in values:\n        print(value)\n    return len(values)\n"
        self.task = {
            "task_id": "local/1", "dataset": "humaneval", "kind": "humaneval",
            "description": "Count values.", "test": "def check(candidate):\n    assert candidate([1]) == 1",
            "entry_point": "solve",
        }
        self.diag = {"status": "ok", "slowest": {
            "idx": 0, "ms": 2.5, "src": "assert candidate([1]) == 1"}}
        self.hot = {"ok": True, "mode": "line_hits", "items": [
            {"line": 3, "hits": 10, "share": 0.5, "src": "print(value)"}]}
        self.timing = {"ok": True, "status": "ok", "mode": "line_profiler",
                       "tool": "line_profiler", "tool_version": "test-double", "unit": "ms",
                       "source_sha256": hashlib.sha256(self.code.encode()).hexdigest(),
                       "items": [{"line": 3, "hits": 10, "cumulative_ms": 2.0,
                                  "src": "        print(value)"}]}

    def build(self, arm, **kwargs):
        if arm == "f4":
            kwargs.setdefault("line_timing", self.timing)
        return build_feedback(
            arm, self.task, self.code, total_ms=5.0,
            diagnostic=self.diag, hot=self.hot, **kwargs,
        )

    def test_five_arms_share_exact_prompt_frame(self):
        results = [self.build(f"f{i}") for i in range(5)]
        frames = [result["prompt"].replace(result["feedback"], "<FEEDBACK>", 1)
                  for result in results]
        self.assertEqual(len(set(frames)), 1)
        self.assertEqual([r["requested_method"] for r in results],
                         ["vanilla", "f1", "f2", "f3", "ours"])
        self.assertNotEqual(len({r["feedback"] for r in results}), 1)
        self.assertEqual([r["requested_arm"] for r in results],
                         ["f0", "f1", "f2", "f3", "f4"])
        self.assertTrue(all(r["requested_arm"] == r["effective_arm"] for r in results))
        self.assertTrue(all(r["eligible_for_requested_method"] for r in results[:4]))
        self.assertFalse(results[4]["eligible_for_requested_method"])
        self.assertTrue(results[4]["schema_mapping_pending"])
        self.assertIsNone(results[4]["method"])
        self.assertFalse(results[4]["can_write_result"])

    def test_full_description_comes_from_task_and_is_required(self):
        schema_only_task = {key: value for key, value in self.task.items() if key != "description"}
        with self.assertRaisesRegex(ValueError, "Task.description is required"):
            build_feedback("f0", schema_only_task, self.code)
        with self.assertRaisesRegex(ValueError, "Task.description is required"):
            build_feedback("f0", {**schema_only_task, "description": "   "}, self.code)
        result = build_feedback(
            "f0", {**schema_only_task, "description": "Full statement with examples and constraints."},
            self.code,
        )
        self.assertIn("Full statement with examples and constraints.", result["prompt"])
        self.assertNotIn("local/1", result["prompt"])

    def test_requested_and_effective_methods_differ_after_downgrade(self):
        result = build_feedback(
            "f4", self.task, self.code,
            diagnostic={"status": "timeout", "error": "budget"}, total_ms=5.0,
        )
        self.assertEqual(result["requested_method"], "ours")
        self.assertEqual(result["requested_arm"], "f4")
        self.assertIsNone(result["method"])
        self.assertEqual(result["effective_method"], "f1")
        self.assertTrue(result["schema_mapping_pending"])
        self.assertFalse(result["eligible_for_requested_method"])

    def test_f1_uses_supplied_whole_task_time(self):
        self.assertIn("5.0000 ms", self.build("f1")["feedback"])
        self.assertNotIn("2.5000 ms", self.build("f1")["feedback"])

    def test_f3_reports_hits_without_claiming_line_time(self):
        result = self.build("f3")
        self.assertIn("L3: 10 hits", result["feedback"])
        self.assertNotIn("2.5000 ms", result["feedback"])
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_hits")

    def test_f4_line_numbers_source_and_scope(self):
        result = self.build("f4", radius=1)
        self.assertIn("2.000000 ms cumulative", result["feedback"])
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_profiler")
        self.assertIn("   3:         print(value)", result["feedback"])
        self.assertIn("for value in values:", result["feedback"])
        self.assertIn("def solve(values):", result["feedback"])
        self.assertEqual(result["diagnostics"]["regions"],
                         [{"start": 2, "end": 4, "hits": [3]}])

    def test_f4_timing_failure_uses_line_hits_without_false_timing(self):
        for status in ("missing_dependency", "timeout", "code_error", "line_mismatch"):
            with self.subTest(status=status):
                result = build_feedback(
                    "f4", self.task, self.code,
                    diagnostic=self.diag, hot=self.hot,
                    line_timing={"ok": False, "status": status, "reason": status},
                    radius=1,
                )
                self.assertEqual(result["requested_method"], "ours")
                self.assertEqual(result["effective_method"], "f4_line_hits")
                self.assertFalse(result["eligible_for_requested_method"])
                self.assertIsNone(result["method"])
                self.assertTrue(result["schema_mapping_pending"])
                self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_hits")
                self.assertIn("line_timing_failed: " + status, result["diagnostics"]["reasons"])
                self.assertIn("L3: 10 hits", result["feedback"])
                self.assertNotIn("ms cumulative", result["feedback"])

    def test_f4_rejects_stale_line_timing(self):
        stale = {**self.timing, "source_sha256": "wrong"}
        result = build_feedback(
            "f4", self.task, self.code,
            diagnostic=self.diag, hot=self.hot, line_timing=stale, radius=1,
        )
        self.assertEqual(result["effective_method"], "f4_line_hits")
        self.assertEqual(result["diagnostics"]["line_timing"]["status"], "line_mismatch")
        self.assertIn("source_hash_mismatch", result["diagnostics"]["reasons"][0])

    def test_f4_context_tracks_changed_input_code(self):
        edited = self.code.replace("print(value)", "print(value * 2)")
        result = build_feedback("f4", self.task, edited,
                                diagnostic=self.diag,
                                line_timing={**self.timing,
                                             "source_sha256": hashlib.sha256(edited.encode()).hexdigest(),
                                             "items": [{**self.timing["items"][0],
                                                                       "src": "        print(value * 2)"}]})
        self.assertIn("print(value * 2)", result["feedback"])
        self.assertNotIn("   3:         print(value)\n", result["feedback"])

    def test_overlapping_and_adjacent_windows_merge(self):
        code = "\n".join(f"x{i} = {i}" for i in range(1, 21))
        regions = context_regions(code, [5, 7, 12], radius=2)
        self.assertEqual([(r["start"], r["end"]) for r in regions], [(3, 14)])
        self.assertEqual(regions[0]["source"].count("   7: "), 1)

    def test_file_boundaries_are_clamped(self):
        code = "x = 1\ny = 2\nz = 3"
        regions = context_regions(code, [1, 3], radius=8)
        self.assertEqual([(r["start"], r["end"]) for r in regions], [(1, 3)])
        self.assertIn("   1: x = 1", regions[0]["source"])
        self.assertIn("   3: z = 3", regions[0]["source"])

    def test_failed_case_diagnosis_downgrades_and_records_reason(self):
        result = build_feedback(
            "f4", self.task, self.code, total_ms=5.0,
            diagnostic={"status": "timeout", "error": "budget"},
        )
        self.assertEqual(result["diagnostics"]["effective"], "f1")
        self.assertIn("case_diagnostic_failed: budget", result["diagnostics"]["reasons"])
        self.assertIn("5.0000 ms", result["feedback"])

    def test_failed_hotspot_and_case_exception_downgrade(self):
        result = build_feedback(
            "f4", self.task, self.code, diagnostic=self.diag,
            line_timing={"ok": False, "status": "missing_dependency", "reason": "not installed"},
            hot={"ok": True, "error": "ValueError: bad case", "items": self.hot["items"]},
        )
        self.assertEqual(result["diagnostics"]["effective"], "f2")
        self.assertIn("Slowest test case:", result["feedback"])
        self.assertEqual(result["method"], "f2")
        self.assertEqual(result["requested_method"], "ours")
        self.assertEqual(result["degrade_reason"], "fallback_f2")
        with patch("optimize.feedback.harness.measure_cases", side_effect=RuntimeError("broken")):
            failed = build_feedback("f2", self.task, self.code)
        self.assertEqual(failed["diagnostics"]["effective"], "none")
        self.assertIn("RuntimeError: broken", failed["diagnostics"]["reasons"][0])

    def test_malformed_diagnostic_payload_downgrades(self):
        bad_case = build_feedback(
            "f4", self.task, self.code,
            diagnostic={"status": "ok", "slowest": {"src": "x"}},
        )
        self.assertEqual(bad_case["diagnostics"]["effective"], "none")
        bad_hot = build_feedback(
            "f4", self.task, self.code, diagnostic=self.diag,
            line_timing={"ok": False, "status": "missing_dependency", "reason": "not installed"},
            hot={"ok": True, "items": [{"line": 3}]},
        )
        self.assertEqual(bad_hot["diagnostics"]["effective"], "f2")

    def test_f4_feedback_budget(self):
        code = "def solve(values):\n" + "\n".join(
            f"    x{i} = {i}  # extra context" for i in range(40)
        )
        hot = {"ok": True, "items": [{"line": 20, "hits": 50}]}
        result = build_feedback(
            "f4", self.task, code,
            line_timing={"ok": False, "status": "missing_dependency", "reason": "not installed"},
            diagnostic=self.diag, hot=hot, max_chars=300,
        )
        self.assertLessEqual(len(result["feedback"]), 300)
        self.assertTrue(result["diagnostics"]["truncated"])
        self.assertEqual(result["effective_method"], "f3")
        self.assertFalse(result["eligible_for_requested_method"])
        self.assertEqual(result["method"], "f3")
        self.assertEqual(result["requested_method"], "ours")
        self.assertEqual(result["degrade_reason"], "budget_exceeded")
        self.assertTrue(result["can_write_result"])
        self.assertEqual(result["diagnostics"]["retained_hot_lines"], [])
        self.assertIn("f4_context_omitted_by_length_limit", result["diagnostics"]["reasons"])
        self.assertNotIn("Source lines", result["feedback"])

    def test_truncated_f4_retains_hotspot_and_nearby_source(self):
        code = "def solve(values):\n" + "\n".join(
            f"    x{i} = {i}  # extra context" for i in range(40)
        )
        hot = {"ok": True, "items": [{"line": 3, "hits": 50}, {"line": 35, "hits": 20}]}
        result = build_feedback(
            "f4", self.task, code,
            line_timing={"ok": False, "status": "missing_dependency", "reason": "not installed"},
            diagnostic=self.diag, hot=hot, max_chars=600,
        )
        self.assertTrue(result["diagnostics"]["truncated"])
        self.assertEqual(result["effective_method"], "f4_line_hits")
        self.assertFalse(result["eligible_for_requested_method"])
        self.assertIn(3, result["diagnostics"]["retained_hot_lines"])
        self.assertIn("L3: 50 hits", result["feedback"])
        self.assertIn("   3:     x1 = 1  # extra context", result["feedback"])
        self.assertIn("   2:     x0 = 0  # extra context", result["feedback"])
        self.assertLessEqual(len(result["feedback"]), 600)

    def test_offline_humaneval_40_and_55(self):
        tasks = {item["task_id"]: item for item in json.loads(DATASET.read_text(encoding="utf-8"))}
        for task_id in ("HumanEval/40", "HumanEval/55"):
            with self.subTest(task_id=task_id):
                raw = tasks[task_id]
                task = {
                    "task_id": task_id, "dataset": "humaneval", "kind": "humaneval",
                    "description": raw["prompt"], "test": raw["test"],
                    "entry_point": raw["entry_point"],
                }
                code = raw["prompt"] + raw["canonical_solution"]
                result = build_feedback("f4", task, code)
                self.assertIn(result["effective_method"], ("f4", "f4_line_hits"),
                              result["diagnostics"]["reasons"])
                self.assertTrue(result["diagnostics"]["hot_lines"])
                self.assertIn("Source lines", result["feedback"])
                self.assertNotIn("ms per line", result["feedback"])


if __name__ == "__main__":
    unittest.main()
