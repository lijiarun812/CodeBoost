"""Offline C/D gate integration checks; no model or formal Result writer runs."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval.gate import check
from optimize.feedback import build_feedback
from optimize.gate_report import rss_evidence
from optimize.model_adapter import CandidateResponse
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

    def test_complete_constructed_f4_is_blocked_by_g6(self):
        output, harness = self.run_case(self.builder())
        row = output["rounds"][0]
        self.assertEqual(row["decision"], "accept")
        self.assertFalse(row["gate"]["passed"])
        self.assertEqual({x["code"] for x in row["gate"]["failures"]}, {"G6"})
        self.assertFalse(row["gate"]["formal_task"])
        self.assertEqual(row["gate"]["suggested_method"], "diagnostic")
        self.assertTrue(row["gate"]["c_feedback_complete"])
        self.assertIsNone(row["feedback"]["method"])
        self.assertIsNone(row["feedback"]["degrade_reason"])
        self.assertEqual(row["feedback"]["effective_method"], "f4")
        self.assertEqual(row["formal_block_reason"], "G6_non_formal_task")
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertEqual(row["feedback"]["diagnostics"]["profiler_overhead_ratio"], 2.4)
        self.assertNotEqual(row["prompt_hashes"]["f3"], row["prompt_hashes"]["f4"])
        self.assertIn("Case execution time: 2.5000 ms", row["feedback_text"])
        self.assertIn("Hot lines (占已剖析行时间之和", row["feedback_text"])
        self.assertNotIn("ms", row["feedback_text"].split("Hot lines", 1)[1])
        self.assertEqual(harness.calls[1]["method"], "diagnostic")

    def test_below_floor_space_uses_na_but_g6_blocks_ours(self):
        output, _ = self.run_case(self.builder(), heap_mb=0.001,
                                  candidate_heap_mb=0.001)
        row = output["rounds"][0]
        self.assertEqual(row["feedback"]["diagnostics"]["space_status"], "below_floor")
        self.assertIn("n/a（本题空间不可分辨", row["feedback_text"])
        self.assertEqual({x["code"] for x in row["gate"]["failures"]}, {"G6"})
        self.assertIsNone(row["feedback"]["method"])

    def test_missing_program_peak_is_explicit_na(self):
        output, _ = self.run_case(self.builder(), heap_mb=None,
                                  candidate_heap_mb=8.0)
        row = output["rounds"][0]
        self.assertEqual(row["feedback"]["diagnostics"]["space_status"], "unmeasured")
        self.assertIn("peak_tracemalloc_mb：n/a", row["feedback_text"])
        self.assertIn("G6", {x["code"] for x in row["gate"]["failures"]})
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
        self.assertEqual(row["suggested_rerun_method"], "diagnostic")

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
        self.assertFalse(row["gate"]["passed"])
        self.assertIn("G6", {x["code"] for x in row["gate"]["failures"]})
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
            prompt_hash_f4=row["prompt_hashes"]["f4"],
            formal_task=False, method="ours",
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

    def test_rss_reason_distinguishes_missing_psutil_from_unknown_sampling(self):
        result = {"mem": {"peak_rss_mb": None}}
        unavailable = rss_evidence(result, psutil_available=False,
                                   python_executable="harness-python")
        self.assertEqual(unavailable["rss_status"], "psutil_unavailable")
        self.assertEqual(unavailable["memory_round_status"], "unknown")
        available = rss_evidence(result, psutil_available=True,
                                 python_executable="harness-python")
        self.assertEqual(available["rss_status"], "rss_not_obtained")
        self.assertIn("cause_unknown", available["reason"])
        self.assertIsNone(available["peak_rss_mb"])

    def test_claimed_formal_task_id_cannot_bypass_g6_or_write_ours(self):
        task = {**self.task, "formal_task": True, "dataset": "humaneval"}
        writes = []
        output = run_offline_ours(
            task, self.frozen, mode="joint", feedback_method="f4",
            generate_candidate=lambda *args: "candidate-v1",
            model="mock", model_version="offline", prompt_version="gate-v1",
            environment_id="offline-env",
            measure_task_fn=FakeHarness({"base": {}, "candidate-v1": {
                "time_ms": 80, "heap_mb": 8}}),
            feedback_builder=self.builder(),
            ensure_rss_baseline_fn=lambda: {"status": "ok", "value": 95.0,
                                            "reason": "mock", "meta": {}},
            write_result=writes.append, allow_formal_results=True,
        )
        row = output["rounds"][0]
        self.assertEqual({x["code"] for x in row["gate"]["failures"]}, {"G6"})
        self.assertEqual(row["gate"]["suggested_method"], "diagnostic")
        self.assertFalse(row["feedback"]["can_write_result"])
        self.assertIsNone(row["feedback"]["method"])
        self.assertEqual(output["formal_results_written"], 0)
        self.assertEqual(writes, [])

    def test_temp_formal_writer_requires_pinned_b_pair_and_complete_gate(self):
        # A temporary schema-shaped B fixture is an interface test, not a
        # claim that the real benchmark has been frozen or measured.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "mock_b_frozen_tasks.jsonl"
            manifest_path.write_text(json.dumps(self.task) + "\n", encoding="utf-8")
            approved = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            bank_manifest_path = root / "bank_manifest.json"
            bank_manifest_path.write_text(json.dumps({
                "dataset": self.task["dataset"], "protocol_version": "proto-2026.11",
                "counts": {"valid_tasks": 1}, "output": manifest_path.name,
                "status": "frozen", "task_bank_sha256": approved,
                "frozen_task_ids": [self.task["task_id"]],
            }), encoding="utf-8")
            approved_bank_manifest = hashlib.sha256(bank_manifest_path.read_bytes()).hexdigest()
            source = FakeHarness({"base": {}, "candidate-v1": {
                "time_ms": 80, "heap_mb": 8}})

            def measured(task, **kwargs):
                result = source(task, **kwargs)
                result.update({
                    "run_id": "offline-mock-run", "dataset": task["dataset"],
                    "started_at": "2026-11-28T00:00:00", "error": None,
                    "manifest": {"model": kwargs["model"],
                                 "model_version": kwargs["model_version"],
                                 "temperature": 0.2, "seed": 42,
                                 "dataset_hash": "mock-fixture", "harness_commit": "mock",
                                 "protocol_version": "proto-2026.11"},
                    "speedup": None, "mem_ratio": None,
                    "downgraded": False, "protocol_version": "proto-2026.11",
                    "code_source": "candidate" if kwargs.get("code") else "canonical",
                })
                result["time"].update({"median_ms": result["time"]["mean_trimmed_ms"],
                                       "iqr": 0.0, "raw_ms": [result["time"]["mean_trimmed_ms"]]})
                return result

            def run(builder, *, digest=approved, filename="formal.json",
                    measure_fn=measured, generator=None):
                path = root / filename
                diagnostic_path = root / (filename + ".diagnostic.json")
                output = run_offline_ours(
                    self.task, self.frozen, mode="joint", feedback_method="f4",
                    generate_candidate=generator or (lambda *args: CandidateResponse(
                        "candidate-v1", "mock-provider", "returned-v7",
                        {"temperature": 0.2})),
                    model="requested-provider", model_version="requested-v1",
                    prompt_version="feedback-v1", environment_id="offline-env",
                    generation_parameters={"temperature": 0.2},
                    frozen_manifest_path=manifest_path, approved_frozen_sha256=digest,
                    bank_manifest_path=bank_manifest_path,
                    approved_bank_manifest_sha256=approved_bank_manifest,
                    measure_task_fn=measure_fn, feedback_builder=builder,
                    ensure_rss_baseline_fn=lambda: {"status": "ok", "value": 95.0,
                                                    "reason": "mock", "meta": {}},
                    write_diagnostic=lambda record: diagnostic_path.write_text(
                        json.dumps(record), encoding="utf-8"),
                    write_result=lambda record: path.write_text(json.dumps(record), encoding="utf-8"),
                    allow_formal_results=True,
                )
                self.assertTrue(diagnostic_path.exists())
                self.assertEqual(json.loads(diagnostic_path.read_text(encoding="utf-8"))["kind"],
                                 "diagnostic_demo")
                return output, path

            good, formal_path = run(self.builder())
            self.assertTrue(good["frozen_task_verification"]["verified"])
            self.assertTrue(good["rounds"][0]["gate"]["passed"])
            self.assertEqual(good["formal_results_written"], 1)
            formal = json.loads(formal_path.read_text(encoding="utf-8"))
            self.assertEqual((formal["method"], formal["requested_method"],
                              formal["degrade_reason"]), ("ours", "ours", None))
            self.assertEqual(formal["manifest"]["model_version"], "returned-v7")
            self.assertEqual(formal["time"]["tiers"][-1]["args_hash"],
                             self.frozen["args_hash"])

            unpinned, path = run(self.builder(), digest="0" * 64,
                                 filename="hash_mismatch.json")
            self.assertFalse(path.exists())
            self.assertIn("G6", {f["code"] for f in unpinned["rounds"][0]["gate"]["failures"]})

            downgraded, path = run(self.builder(slow_ms=0.3), filename="downgraded.json")
            self.assertFalse(path.exists())
            self.assertFalse(downgraded["rounds"][0]["feedback"]["can_write_result"])

            original = self.builder()
            def identical_prompts(method, task, code, **kwargs):
                fb = original("f3", task, code, **kwargs)
                if method == "f4":
                    fb.update({"requested_method": "ours", "effective_method": "f4",
                               "method": None, "can_write_result": False})
                    fb["diagnostics"].update({"hotspot_mode": "line_profiler",
                        "case_ms": 2.5, "profiler_overhead_ratio": 2.4,
                        "space_status": "measured", "feedback_complete": True,
                        "line_timing": {"numbers_in": "hits_only"}})
                return fb
            bad_gate, path = run(identical_prompts, filename="gate_failed.json")
            self.assertFalse(path.exists())
            self.assertIn("G5", {f["code"] for f in bad_gate["rounds"][0]["gate"]["failures"]})

            legacy, path = run(self.builder(), filename="legacy_code_only.json",
                               generator=lambda *args: "candidate-v1")
            self.assertFalse(path.exists())
            self.assertTrue(legacy["rounds"][0]["gate"]["passed"])
            self.assertIn("actual_model_response_unavailable",
                          legacy["rounds"][0]["formal_write_blockers"])

            def wrong_pair(task, **kwargs):
                result = measured(task, **kwargs)
                if kwargs.get("code"):
                    result["time"]["tiers"][-1]["args_hash"] = "wrong-hash"
                return result
            mismatched, path = run(self.builder(), filename="pair_mismatch.json",
                                   measure_fn=wrong_pair)
            self.assertFalse(path.exists())
            self.assertEqual(mismatched["rounds"][0]["reason"], "pair_mismatch")

    def test_committed_measured_report_is_utf8_without_bom_and_diagnostic(self):
        path = Path(__file__).resolve().parents[1] / "results" / "gate_f4.json"
        self.assertNotEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
        with path.open(encoding="utf-8") as stream:
            report = json.load(stream)
        self.assertEqual(report["task"]["task_id"], "diagnostic/loop200k")
        self.assertFalse(report["classification"]["frozen_benchmark_sample"])
        self.assertFalse(report["classification"]["formal_ours_result"])
        self.assertIsNone(report["raw_harness_result"]["mem"]["peak_rss_mb"])
        self.assertFalse(report["f4_gate"]["passed"])
        self.assertFalse(report["gate_invocation"]["formal_task"])
        self.assertEqual(report["rss_diagnostic"]["rss_status"],
                         "psutil_unavailable")
        self.assertEqual(report["f4_gate"]["suggested_method"], "diagnostic")
        self.assertIn("G6", {x["code"] for x in report["f4_gate"]["failures"]})


if __name__ == "__main__":
    unittest.main()
