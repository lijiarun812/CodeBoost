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


def _schema_space_pair(task, *, tm=12.5, rss=100.0, args_hash="frozen-hash",
                       status="ok"):
    frozen = {"tier": "t2", "n": 100, "gen": "seed:1", "args_hash": "frozen-hash",
              "test": task["test"]}
    paired_task = {**task, "code": "def solve(values): return len(values)",
                   "inputs": [frozen]}
    result = {
        "run_id": "offline-space-result", "task_id": task["task_id"],
        "dataset": task["dataset"], "method": "base",
        "started_at": "2026-11-27T00:00:00",
        "manifest": {"model": "canonical", "model_version": "offline",
                     "temperature": 0.2, "seed": 42,
                     "dataset_hash": "sha256:offline", "harness_commit": "offline",
                     "protocol_version": "proto-2026.11"},
        "status": status, "error": None,
        "time": {"mean_trimmed_ms": 5.0, "median_ms": 5.0, "iqr": 0.0,
                 "cv": 0.01, "raw_ms": [5.0],
                 "tiers": [{"tier": "t2", "n": 100, "gen": "seed:1",
                            "args_hash": args_hash, "status": status,
                            "median_ms": 5.0, "iqr": 0.0, "cv": 0.01,
                            "reps": 1, "downgraded": False}]},
        "mem": {"peak_tracemalloc_mb": tm, "peak_rss_mb": rss},
        "speedup": None, "mem_ratio": None, "downgraded": False,
        "protocol_version": "proto-2026.11", "code_source": "canonical",
    }
    return paired_task, result


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
                       "unprofiled_case_wall_ms": 2.0,
                       "profiled_case_wall_ms": 5.0,
                       "overhead_method": "same_child_fresh_namespace_case_wall",
                       "source_sha256": hashlib.sha256(self.code.encode()).hexdigest(),
                       "items": [{"line": 3, "hits": 10, "line_time_ms": 2.0,
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

    def test_program_space_feedback_uses_official_result_and_frozen_tier(self):
        task, measurement = _schema_space_pair(self.task)
        results = [build_feedback(
            f"f{i}", task, self.code, total_ms=5.0, diagnostic=self.diag,
            hot=self.hot, line_timing=self.timing, space_result=measurement,
            rss_baseline_mb=95.0,
        ) for i in range(5)]
        frames = [item["prompt"].replace(item["feedback"], "<FEEDBACK>", 1)
                  for item in results]
        self.assertEqual(len(set(frames)), 1)
        for item in results[:4]:
            self.assertNotIn("peak_tracemalloc_mb", item["feedback"])
        f4 = results[4]
        self.assertIn("eval.harness.measure_task", f4["feedback"])
        self.assertIn("独立内存测量", f4["feedback"])
        self.assertIn("单位 MB", f4["feedback"])
        self.assertIn("args_hash=frozen-hash", f4["feedback"])
        self.assertIn("peak_tracemalloc_mb：12.5000 MB", f4["feedback"])
        self.assertIn("peak_rss_mb：100.000 MB", f4["feedback"])
        self.assertIn("仅作资源报告／门禁，不作为空间改善奖励", f4["feedback"])
        self.assertIn("不是逐行内存归因", f4["feedback"])
        self.assertEqual(f4["diagnostics"]["space"]["peak_tm_status"], "measured")
        self.assertEqual(f4["diagnostics"]["space"]["rss_status"], "gated")
        self.assertFalse(f4["can_write_result"])

    def test_program_space_below_floor_and_rss_indistinguishable(self):
        task, measurement = _schema_space_pair(self.task, tm=0.001, rss=20.7)
        result = build_feedback("f4", task, self.code, diagnostic=self.diag,
                                line_timing=self.timing, space_result=measurement,
                                rss_baseline_mb=20.5)
        self.assertEqual(result["diagnostics"]["space"]["peak_tm_status"], "below_floor")
        self.assertEqual(result["diagnostics"]["space"]["rss_status"], "indistinguishable")
        self.assertIn("低于 0.05 MB 分辨下限", result["feedback"])
        self.assertIn("20.700 MB（indistinguishable：与有效空载基线差异不足，门禁跳过）",
                      result["feedback"])
        self.assertFalse(result["can_write_result"])

    def test_program_space_missing_peaks_mean_unmeasured_not_gain(self):
        task, measurement = _schema_space_pair(self.task, tm=None, rss=None)
        result = build_feedback("f4", task, self.code, diagnostic=self.diag,
                                line_timing=self.timing, space_result=measurement)
        self.assertEqual(result["diagnostics"]["space"]["peak_tm_status"], "unmeasured")
        self.assertEqual(result["diagnostics"]["space"]["rss_status"], "unavailable")
        self.assertIn("本次独立内存测量未取得峰值", result["feedback"])
        self.assertIn("RSS 门禁跳过；扣基线 MU 为 n/a", result["feedback"])
        self.assertNotIn("堆峰值改善", result["feedback"])
        self.assertFalse(result["can_write_result"])

    def test_program_space_without_valid_rss_baseline_marks_mu_unavailable(self):
        task, measurement = _schema_space_pair(self.task, rss=100.0)
        result = build_feedback("f4", task, self.code, diagnostic=self.diag,
                                line_timing=self.timing, space_result=measurement,
                                rss_baseline_mb=None)
        self.assertEqual(result["diagnostics"]["space"]["rss_status"], "unavailable")
        self.assertIsNone(result["diagnostics"]["space"]["rss_baseline_mb"])
        self.assertIn("100.000 MB（unavailable：缺少有效空载基线", result["feedback"])
        self.assertIn("扣基线 MU 为 n/a", result["feedback"])
        self.assertFalse(result["can_write_result"])

    def test_invalid_rss_baseline_is_not_silently_used(self):
        task, measurement = _schema_space_pair(self.task)
        with self.assertRaisesRegex(ValueError, "rss_baseline_mb"):
            build_feedback("f4", task, self.code, diagnostic=self.diag,
                           line_timing=self.timing, space_result=measurement,
                           rss_baseline_mb=0)

    def test_program_space_mismatched_tier_is_not_presented_as_evidence(self):
        task, measurement = _schema_space_pair(self.task, args_hash="wrong")
        result = build_feedback("f4", task, self.code, diagnostic=self.diag,
                                line_timing=self.timing, space_result=measurement)
        self.assertEqual(result["diagnostics"]["space"]["reason"], "frozen_tier_mismatch")
        self.assertIn("冻结输入档与题库不一致", result["feedback"])
        self.assertNotIn("12.5000 MB", result["feedback"])
        self.assertFalse(result["can_write_result"])

    def test_program_space_is_omitted_when_f4_degrades_to_f2(self):
        task, measurement = _schema_space_pair(self.task)
        result = build_feedback(
            "f4", task, self.code, diagnostic=self.diag, space_result=measurement,
            line_timing={"ok": False, "status": "timeout", "reason": "timed out"},
            hot={"ok": False, "reason": "hotspot timeout"},
        )
        self.assertEqual(result["effective_method"], "f2")
        self.assertEqual(result["degrade_reason"], "fallback_f2")
        self.assertEqual(result["diagnostics"]["space"]["status"],
                         "omitted_after_feedback_downgrade")
        self.assertNotIn("peak_tracemalloc_mb", result["feedback"])

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
        self.assertEqual(result["method"], "f1")
        self.assertEqual(result["effective_method"], "f1")
        self.assertFalse(result["schema_mapping_pending"])
        self.assertEqual(result["degrade_reason"], "no_hotspot")
        self.assertFalse(result["eligible_for_requested_method"])

    def test_f1_uses_supplied_whole_task_time(self):
        self.assertIn("5.0000 ms", self.build("f1")["feedback"])
        self.assertNotIn("2.5000 ms", self.build("f1")["feedback"])

    def test_f3_reports_hits_without_claiming_line_time(self):
        result = self.build("f3")
        self.assertIn("L3: 10 hits", result["feedback"])
        self.assertIn("Case execution time: 2.5000 ms", result["feedback"])
        self.assertNotIn("2.5000 ms", result["feedback"].split("Hot lines", 1)[1])
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_hits")

    def test_f4_line_numbers_source_and_scope(self):
        result = self.build("f4", radius=1)
        self.assertIn("Hot lines (占已剖析行时间之和", result["feedback"])
        self.assertIn("L3: 100.0% | 10 hits", result["feedback"])
        self.assertEqual(result["diagnostics"]["profiler_overhead_ratio"], 2.5)
        self.assertEqual(result["diagnostics"]["line_timing"]["numbers_in"], "percent")
        self.assertNotIn("逐行耗时相对占比", result["feedback"])
        self.assertNotIn("% of profiled lines", result["feedback"])
        self.assertNotIn("2.000000 ms", result["feedback"])
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_profiler")
        self.assertIn("   3:         print(value)", result["feedback"])
        self.assertIn("for value in values:", result["feedback"])
        self.assertIn("def solve(values):", result["feedback"])
        self.assertEqual(result["diagnostics"]["regions"],
                         [{"start": 2, "end": 4, "hits": [3]}])

    def test_timing_timeout_scales_with_slowest_case(self):
        slow = {"status": "ok", "mode": "block_split", "cases": [
            {"idx": 0, "ms": 1500.0, "src": "assert candidate([1]) == 1"}],
            "slowest": {"idx": 0, "ms": 1500.0, "src": "assert candidate([1]) == 1"}}
        with patch("optimize.feedback.profile_case", return_value={
            "ok": False, "status": "timeout", "reason": "timed out",
        }) as profile:
            result = build_feedback("f4", self.task, self.code,
                                    diagnostic=slow, hot=self.hot)
        self.assertEqual(profile.call_args.kwargs["timeout_s"], 30.0)
        self.assertEqual(result["diagnostics"]["slow_ms"], 1500.0)
        self.assertEqual(result["diagnostics"]["case_mode"], "block_split")
        self.assertEqual(result["diagnostics"]["case_count"], 1)
        self.assertEqual(result["diagnostics"]["diagnostic_timeout_s"], 30.0)
        self.assertEqual(result["diagnostics"]["diagnostic_timeout_cap_s"], 60.0)
        self.assertEqual(result["degrade_reason"], "no_hotspot")
        self.assertFalse(result["can_write_result"])

    def test_f3_and_f4_timeout_share_a_hard_cap(self):
        slow = {"status": "ok", "slowest": {
            "idx": 0, "ms": 5000.0, "src": "assert candidate([1]) == 1"}}
        with patch("optimize.feedback.hotspots", return_value=self.hot) as hotspot:
            f3 = build_feedback("f3", self.task, self.code, diagnostic=slow)
        self.assertEqual(hotspot.call_args.kwargs["max_time"], 60.0)
        self.assertEqual(f3["diagnostics"]["slow_ms"], 5000.0)
        self.assertEqual(f3["diagnostics"]["diagnostic_timeout_s"], 60.0)
        with patch("optimize.feedback.profile_case", return_value={
            "ok": False, "status": "timeout", "reason": "diagnostic timeout",
        }) as profile:
            f4 = build_feedback("f4", self.task, self.code,
                                diagnostic=slow, hot=self.hot)
        self.assertEqual(profile.call_args.kwargs["timeout_s"], 60.0)
        self.assertEqual(f4["diagnostics"]["diagnostic_timeout_s"], 60.0)
        with self.assertRaisesRegex(ValueError, "at most 60 seconds"):
            build_feedback("f3", self.task, self.code, line_timeout_s=61.0,
                           diagnostic=slow, hot=self.hot)

    def test_diagnostic_failure_does_not_change_official_harness_status(self):
        official_result = {"status": "ok", "time": {"mean_trimmed_ms": 5.0}}
        result = build_feedback(
            "f4", self.task, self.code,
            total_ms=official_result["time"]["mean_trimmed_ms"],
            diagnostic=self.diag, hot=self.hot,
            line_timing={"ok": False, "status": "code_error",
                         "reason": "probe environment failed"},
        )
        self.assertEqual(official_result["status"], "ok")
        self.assertEqual(result["diagnostics"]["line_timing"]["status"], "code_error")
        self.assertNotIn("status", result)
        self.assertNotIn("result_status_hint", result["diagnostics"]["line_timing"])
        self.assertEqual(result["degrade_reason"], "no_hotspot")

    def test_f3_diagnostic_timeout_falls_to_f2_without_formal_status(self):
        with patch("optimize.feedback.hotspots", return_value={
            "ok": False, "reason": "timeout(10s)", "mode": "none",
        }) as hotspot:
            result = build_feedback("f3", self.task, self.code,
                                    diagnostic=self.diag)
        self.assertEqual(hotspot.call_args.kwargs["max_time"], 10.0)
        self.assertEqual(result["method"], "f2")
        self.assertEqual(result["degrade_reason"], "fallback_f2")
        self.assertEqual(result["diagnostics"]["effective"], "f2")
        self.assertNotIn("status", result)

    def test_small_case_skips_timing_and_uses_hits(self):
        small = {"status": "ok", "mode": "block_split", "cases": [
            {"idx": 0, "ms": 0.3, "src": "assert candidate([1]) == 1"}],
            "slowest": {"idx": 0, "ms": 0.3, "src": "assert candidate([1]) == 1"}}
        with patch("optimize.feedback.profile_case") as profile:
            result = build_feedback("f4", self.task, self.code,
                                    diagnostic=small, hot=self.hot)
        profile.assert_not_called()
        self.assertEqual(result["effective_method"], "f3")
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_hits")
        self.assertEqual(result["diagnostics"]["line_timing"]["status"], "case_too_small")
        self.assertEqual(result["diagnostics"]["not_applicable"]["profiler_overhead_ratio"],
                         "case_too_small")
        self.assertEqual(result["diagnostics"]["line_timing"]["numbers_in"], "hits_only")
        self.assertIsNone(result["diagnostics"]["profiler_overhead_ratio"])
        self.assertEqual(result["method"], "f3")
        self.assertEqual(result["degrade_reason"], "no_hotspot")
        self.assertNotIn("ms", result["feedback"].split("Hot lines", 1)[1])
        self.assertNotIn("%", result["feedback"].split("Hot lines", 1)[1])
        self.assertNotIn("占本次已剖析行时间之和的比例", result["feedback"])

    def test_test_block_passes_shared_prelude_to_hotspots(self):
        from eval.harness import PREAMBLE
        task = {**self.task, "kind": "test_block"}
        with patch("optimize.feedback.hotspots", return_value=self.hot) as hotspot:
            result = build_feedback("f3", task, self.code, diagnostic=self.diag)
        self.assertEqual(hotspot.call_args.kwargs["prelude"], PREAMBLE)
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_hits")

    def test_effibench_shaped_block_split_and_typing_prelude(self):
        code = ("class Solution:\n"
                "    def solve(self, values: List[int]) -> int:\n"
                "        return sum(values)\n")
        task = {"task_id": "local/effi", "kind": "test_block", "dataset": "effibench",
                "description": "Sum a list of integers.", "entry_point": "solution",
                "test": "assert solution.solve([1]) == 1\n"
                        "assert solution.solve([1, 2, 3]) == 6\n"}
        result = build_feedback("f4", task, code)
        meta = result["diagnostics"]
        self.assertEqual(meta["case_mode"], "block_split")
        self.assertEqual(meta["case_count"], 2)
        self.assertIn(meta["case"]["src"], task["test"].splitlines())
        self.assertEqual(meta["hotspot_mode"], "line_hits")
        self.assertEqual(result["effective_method"], "f3")
        self.assertEqual(result["degrade_reason"], "no_hotspot")
        self.assertEqual(result["method"], "f3")

    def test_timing_context_cutoff_returns_real_line_hits(self):
        code = "def solve(values):\n" + "\n".join("    value = 1" for _ in range(40))
        timing = {**self.timing,
                  "source_sha256": hashlib.sha256(code.encode()).hexdigest(),
                  "items": [{"line": 20, "hits": 1, "line_time_ms": 3.0,
                             "src": "    value = 1"}]}
        hot = {"ok": True, "items": [{"line": 20, "hits": 50}]}
        result = build_feedback("f4", self.task, code, diagnostic=self.diag,
                                line_timing=timing, hot=hot, max_chars=300)
        self.assertEqual(result["effective_method"], "f3")
        self.assertEqual(result["diagnostics"]["hotspot_mode"], "line_hits")
        self.assertEqual(result["degrade_reason"], "budget_exceeded")
        self.assertIn("L20: 50 hits", result["feedback"])
        self.assertNotIn("占本次已剖析行时间之和的比例", result["feedback"])

    def test_repeated_source_rows_do_not_fake_retained_hotspot_range(self):
        code = "def solve():\n" + "\n".join("    value = 1" for _ in range(40))
        hot = {"ok": True, "items": [{"line": 3, "hits": 50},
                                     {"line": 35, "hits": 20}]}
        result = build_feedback(
            "f4", self.task, code, diagnostic=self.diag, hot=hot,
            line_timing={"ok": False, "status": "missing_dependency",
            "reason": "not installed"}, radius=1, max_chars=430,
        )
        self.assertTrue(result["diagnostics"]["truncated"])
        self.assertEqual(result["diagnostics"]["retained_hot_lines"], [3])
        self.assertIn((2, 4), result["diagnostics"]["retained_source_ranges"])
        self.assertNotIn(35, result["diagnostics"]["retained_hot_lines"])

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
                self.assertNotIn("result_status_hint", result["diagnostics"]["line_timing"])
                self.assertNotIn("status", result)
                self.assertIn("line_timing_failed: " + status, result["diagnostics"]["reasons"])
                self.assertIn("L3: 10 hits", result["feedback"])
                self.assertNotIn("占本次已剖析行时间之和的比例", result["feedback"])

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
        self.assertIn("Slowest test case (measured by harness", result["feedback"])
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
        self.assertFalse(result["can_write_result"])
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
                self.assertIn(result["effective_method"], ("f4", "f4_line_hits", "f3"),
                              result["diagnostics"]["reasons"])
                self.assertTrue(result["diagnostics"]["hot_lines"])
                self.assertIn("Source lines", result["feedback"])
                self.assertNotIn("ms per line", result["feedback"])


if __name__ == "__main__":
    unittest.main()
