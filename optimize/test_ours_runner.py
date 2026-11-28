"""Offline runner tests; all writer effects are confined to temporary dirs."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import rss_baseline
from optimize.feedback import build_feedback
from optimize.frozen_task import verify_frozen_task
from optimize.model_adapter import CandidateResponse
from optimize.ours_runner import run_offline_ours
from optimize.test_ours_pipeline import FakeHarness, _task


class OfflineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.task, self.frozen = _task()
        self.rss_info = {"status": "ok", "value": 95.0, "reason": "measured_now",
                         "meta": {"host": "mock-host", "python": "3.11",
                                  "protocol_version": "proto-2026.11",
                                  "measured_at": "2026-11-28T10:00:00"}}

    def run_case(self, cases, *, mode="joint", feedback_method="f1", **kwargs):
        harness = FakeHarness(cases)
        kwargs.setdefault("ensure_rss_baseline_fn", lambda: dict(self.rss_info))
        output = run_offline_ours(
            self.task, self.frozen, mode=mode, feedback_method=feedback_method,
            generate_candidate=lambda prompt, code, index: "candidate-v1",
            model="mock-model", model_version="offline-v1",
            prompt_version="feedback-v1", environment_id="offline-env",
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
        record = output["rss_baseline_record"]
        self.assertEqual((record["status"], record["reason"], record["value"]),
                         ("ok", "measured_now", 95.0))
        self.assertEqual(record["meta"], self.rss_info["meta"])
        self.assertEqual(record["environment"]["environment_id"], "offline-env")
        self.assertEqual(output["rss_gate_status"], "eligible")
        self.assertIsNone(output["mu_deducted_baseline_mb"])
        self.assertEqual(output["rss_baseline_mb"], 95.0)
        self.assertTrue(all(call["tier_plan"] == [self.frozen] for call in harness.calls))
        self.assertTrue(all(call["kwargs"]["model"] == "mock-model" for call in harness.calls))
        self.assertFalse(output["formal_result_written"])

    def test_valid_baseline_is_checked_once_before_measurement(self):
        order = []
        harness = FakeHarness({"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}})

        def ensure_once():
            order.append("ensure")
            return dict(self.rss_info)

        def measured(*args, **kwargs):
            order.append("measure")
            return harness(*args, **kwargs)

        run_offline_ours(
            self.task, self.frozen, mode="joint", feedback_method="f1",
            generate_candidate=lambda *args: "candidate-v1",
            model="mock", model_version="v1", prompt_version="p1",
            environment_id="offline-env", measure_task_fn=measured,
            ensure_rss_baseline_fn=ensure_once,
        )
        self.assertEqual(order, ["ensure", "measure", "measure"])

    def test_old_format_cache_is_invalid_and_failed_probe_does_not_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "rss_baseline.json"
            cache.write_text(json.dumps({"peak_rss_mb": 95.0}), encoding="utf-8")
            with patch.object(rss_baseline, "CACHE", cache), \
                    patch.object(rss_baseline, "measure", return_value=None):
                output, harness = self.run_case(
                    {"base": {}, "candidate-v1": {
                        "time_ms": 80, "heap_mb": 8, "rss_mb": 130}},
                    ensure_rss_baseline_fn=rss_baseline.ensure,
                )
        self.assertEqual(output["rss_baseline_record"]["status"], "missing")
        self.assertIn("cache_missing_meta", output["rss_baseline_record"]["reason"])
        self.assertIsNone(output["rss_baseline_record"]["value"])
        self.assertIsNone(output["rss_baseline_mb"])
        self.assertEqual(output["mu_deducted_baseline_mb"], "n/a")
        self.assertEqual(output["rss_gate_status"], "skipped")
        self.assertEqual(output["rounds"][0]["decision_detail"]["rss_status"],
                         "unavailable")
        self.assertEqual(output["rounds"][0]["decision"], "accept")
        self.assertEqual(len(harness.calls), 2)

    def test_stale_baseline_value_is_never_used(self):
        stale = {"status": "stale", "value": 95.0, "reason": "stale(8天>7天)",
                 "meta": {"host": "old-host", "python": "3.10"}}
        output, _ = self.run_case(
            {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8,
                                           "rss_mb": 130}},
            ensure_rss_baseline_fn=lambda: stale,
        )
        self.assertEqual(output["rss_baseline_record"]["reason"], stale["reason"])
        self.assertEqual(output["rss_baseline_record"]["meta"], stale["meta"])
        self.assertIsNone(output["rss_baseline_record"]["value"])
        self.assertIsNone(output["rss_baseline_mb"])
        self.assertEqual(output["mu_deducted_baseline_mb"], "n/a")
        self.assertEqual(output["rounds"][0]["decision_detail"]["rss_status"],
                         "unavailable")
        self.assertEqual(output["rounds"][0]["decision"], "accept")

    def test_missing_baseline_and_ensure_exception_continue(self):
        cases = {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8,
                                               "rss_mb": 130}}
        for ensure_fn in (
            lambda: {"status": "missing", "value": None,
                     "reason": "measure_failed", "meta": {"host": "mock-host"}},
            lambda: (_ for _ in ()).throw(OSError("probe failed")),
        ):
            with self.subTest(ensure_fn=ensure_fn):
                output, harness = self.run_case(cases, ensure_rss_baseline_fn=ensure_fn)
                self.assertEqual(output["rounds"][0]["decision"], "accept")
                self.assertEqual(output["rounds"][0]["decision_detail"]["rss_status"],
                                 "unavailable")
                self.assertEqual(output["rss_gate_status"], "skipped")
                self.assertEqual(output["mu_deducted_baseline_mb"], "n/a")
                self.assertIsNone(output["rss_baseline_mb"])
                self.assertTrue(output["rss_baseline_record"]["reason"])
                self.assertEqual(output["rss_baseline_record"]["environment"]["environment_id"],
                                 "offline-env")
                self.assertEqual(len(harness.calls), 2)

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

    def test_missing_baseline_reaches_f4_feedback_as_none(self):
        diagnostic = {"status": "ok", "slowest": {
            "idx": 0, "ms": 0.3, "src": "assert candidate(1) == 1"}}
        seen = []

        def c_feedback(method, task, code, *, total_ms=None,
                       space_result=None, rss_baseline_mb=None):
            seen.append((space_result, rss_baseline_mb))
            return build_feedback(
                method, task, code, total_ms=total_ms, diagnostic=diagnostic,
                hot={"ok": True, "items": [{"line": 1, "hits": 4}]},
                space_result=space_result, rss_baseline_mb=rss_baseline_mb,
            )

        with tempfile.TemporaryDirectory() as directory:
            formal_path = Path(directory) / "result.json"
            output, _ = self.run_case(
                {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}},
                feedback_method="f4", feedback_builder=c_feedback,
                ensure_rss_baseline_fn=lambda: {"status": "missing", "value": None,
                                                "reason": "measure_failed", "meta": {}},
                allow_formal_results=False,
                write_result=lambda record: formal_path.write_text(json.dumps(record)),
            )
            self.assertFalse(formal_path.exists())
        self.assertEqual(len(seen), 2)  # F4 and same-code F3 comparison
        self.assertTrue(all(item[0] is output["rounds"][0]["baseline_measurement"]
                            for item in seen))
        self.assertTrue(all(item[1] is None for item in seen))
        self.assertEqual(output["rounds"][0]["feedback"]["diagnostics"]["space"]["rss_status"],
                         "unavailable")
        self.assertEqual(output["mu_deducted_baseline_mb"], "n/a")
        self.assertFalse(output["formal_result_written"])

    def test_full_f4_writes_diagnostic_only_even_with_result_writer(self):
        diagnostic = {"status": "ok", "slowest": {
            "idx": 0, "ms": 2.5, "src": "assert candidate(1) == 1"}}
        timing = {"ok": True, "status": "ok", "mode": "line_profiler", "unit": "ms",
                  "source_sha256": hashlib.sha256(self.task["code"].encode()).hexdigest(),
                  "items": [{"line": 1, "hits": 1, "line_time_ms": 3.0,
                             "src": self.task["code"]}]}
        for variant in ("f4", "f4_line_hits"):
            with self.subTest(variant=variant):
                case = diagnostic if variant == "f4" else {
                    **diagnostic, "slowest": {**diagnostic["slowest"], "ms": 0.3}}

                def c_feedback(method, task, code, *, total_ms=None,
                               space_result=None, rss_baseline_mb=None):
                    return build_feedback(
                        method, task, code, total_ms=total_ms,
                        diagnostic=case, line_timing=timing,
                        hot={"ok": True, "items": [{"line": 1, "hits": 4}]},
                        space_result=space_result,
                        rss_baseline_mb=rss_baseline_mb,
                    )

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    formal_path = root / "result.json"
                    diagnostic_path = root / "diagnostic.json"
                    output, _ = self.run_case(
                        {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}},
                        feedback_method="f4", feedback_builder=c_feedback,
                        allow_formal_results=False,
                        write_result=lambda record: formal_path.write_text(json.dumps(record)),
                        write_diagnostic=lambda record: diagnostic_path.write_text(json.dumps(record)),
                    )
                    self.assertTrue(diagnostic_path.exists())
                    self.assertFalse(formal_path.exists())
                    self.assertEqual(output["formal_results_written"], 0)
                    self.assertFalse(output["formal_result_written"])
                    feedback = output["rounds"][0]["feedback"]
                    self.assertEqual(feedback["effective_method"],
                                     variant if variant == "f4" else "f3")
                    self.assertFalse(feedback["can_write_result"])
                    self.assertEqual(feedback["diagnostics"]["space"]["rss_baseline_mb"],
                                     self.rss_info["value"])
                    self.assertEqual(json.loads(diagnostic_path.read_text())["kind"],
                                     "diagnostic_demo")

    def test_result_writer_remains_disabled_for_f1_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_path = root / "result.json"
            output, _ = self.run_case(
                {"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}},
                allow_formal_results=False,
                write_result=lambda record: formal_path.write_text(json.dumps(record)),
            )
            self.assertFalse(formal_path.exists())
            self.assertEqual(output["rounds"][0]["feedback"]["method"], "f1")
            self.assertEqual(output["formal_results_written"], 0)

    def test_structured_mock_generation_records_actual_version_and_parameters(self):
        response = CandidateResponse(
            code="candidate-v1", model="provider-returned-model",
            actual_model_version="provider-returned-v2",
            parameters={"temperature": 0.15, "max_tokens": 512},
        )
        harness = FakeHarness({"base": {}, "candidate-v1": {"time_ms": 80, "heap_mb": 8}})
        output = run_offline_ours(
            self.task, self.frozen, mode="joint", feedback_method="f1",
            generate_candidate=lambda *args: response,
            model="requested-model", model_version="requested-v1",
            prompt_version="prompt-v3", environment_id="offline-env",
            generation_parameters={"temperature": 0.2},
            measure_task_fn=harness,
            ensure_rss_baseline_fn=lambda: dict(self.rss_info),
        )
        row = output["rounds"][0]
        self.assertEqual(row["generation"]["candidate_code"], "candidate-v1")
        self.assertEqual(row["model_version"], "provider-returned-v2")
        self.assertEqual(row["actual_model_version"], "provider-returned-v2")
        self.assertEqual(row["model_version_source"], "provider_response")
        self.assertEqual(row["declared_model_version"], "requested-v1")
        self.assertEqual(row["prompt_version"], "prompt-v3")
        self.assertEqual(row["generation"]["parameters"]["max_tokens"], 512)
        self.assertEqual(row["generation_parameters_requested"], {"temperature": 0.2})
        self.assertEqual(harness.calls[0]["kwargs"]["model_version"], "requested-v1")
        self.assertEqual(harness.calls[1]["kwargs"]["model_version"], "provider-returned-v2")
        self.assertEqual(harness.calls[1]["kwargs"]["model"], "provider-returned-model")
        self.assertEqual(harness.calls[1]["kwargs"]["temperature"], 0.15)
        self.assertFalse(output["formal_result_written"])

    def test_provider_declared_failure_reason_is_retained_without_measurement(self):
        harness = FakeHarness({"base": {}})
        outcome = CandidateResponse(
            code=None, model="mock-provider", actual_model_version="v2",
            parameters={"temperature": 0.2}, failure_reason="rate_limit")
        output = run_offline_ours(
            self.task, self.frozen, mode="joint", feedback_method="f1",
            generate_candidate=lambda *args: outcome,
            model="mock-provider", model_version="requested-v1",
            prompt_version="p1", environment_id="offline-env",
            measure_task_fn=harness,
            ensure_rss_baseline_fn=lambda: dict(self.rss_info),
        )
        row = output["rounds"][0]
        self.assertEqual(row["generation"]["failure_reason"], "rate_limit")
        self.assertEqual(row["actual_model_version"], "v2")
        self.assertEqual(row["reason"], "generation_failed")
        self.assertEqual(len(harness.calls), 1)

    def test_generation_error_is_diagnostic_and_skips_candidate_measurement(self):
        harness = FakeHarness({"base": {}})
        output = run_offline_ours(
            self.task, self.frozen, mode="joint", feedback_method="f1",
            generate_candidate=lambda *args: (_ for _ in ()).throw(TimeoutError("do not log secret")),
            model="mock", model_version="v1", prompt_version="p1",
            environment_id="offline-env", measure_task_fn=harness,
            ensure_rss_baseline_fn=lambda: dict(self.rss_info),
        )
        row = output["rounds"][0]
        self.assertEqual(row["reason"], "generation_failed")
        self.assertEqual(row["generation"]["status"], "api_failed")
        self.assertEqual(row["generation"]["failure_reason"],
                         "generator_exception:TimeoutError")
        self.assertNotIn("secret", str(row))
        self.assertIsNone(row["candidate_code"])
        self.assertEqual(len(harness.calls), 1)
        self.assertFalse(output["formal_result_written"])

    def test_pinned_b_manifest_requires_digest_and_matching_input(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "b_frozen_tasks.json"
            manifest.write_text(json.dumps({"tasks": [self.task]}), encoding="utf-8")
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            valid = verify_frozen_task(self.task, self.frozen,
                                       manifest_path=manifest, approved_sha256=digest)
            self.assertTrue(valid["verified"])
            self.assertEqual(valid["reason"], "matched_pinned_b_task_and_input")
            self.assertFalse(verify_frozen_task(self.task, self.frozen,
                manifest_path=manifest, approved_sha256="0" * 64)["verified"])
            wrong = {**self.frozen, "args_hash": "different"}
            self.assertEqual(verify_frozen_task(self.task, wrong,
                manifest_path=manifest, approved_sha256=digest)["reason"],
                "frozen_tier_hash_not_unique_or_missing")
            claimed = {**self.task, "formal_task": True, "code": "different code"}
            self.assertEqual(verify_frozen_task(claimed, self.frozen,
                manifest_path=manifest, approved_sha256=digest)["reason"],
                "frozen_task_content_mismatch")


if __name__ == "__main__":
    unittest.main()
