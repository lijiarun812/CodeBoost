"""Offline line-timing contract tests and optional real-profiler smoke tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from optimize.line_timing import MARKER, profile_case

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets" / "sample_humaneval_20.json"
HAS_LINE_PROFILER = importlib.util.find_spec("line_profiler") is not None


class LineTimingContractTests(unittest.TestCase):
    def test_timeout_is_explicit(self):
        with patch("optimize.line_timing.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["python"], 0.1)):
            result = profile_case("def f(): pass", "f()", "f", timeout_s=0.1)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "none")

    def test_source_hash_and_line_text_must_match(self):
        code = "def f():\n    return 1\n"
        base = {
            "ok": True, "status": "ok", "mode": "line_profiler", "unit": "ms",
            "unprofiled_case_wall_ms": 2.0, "profiled_case_wall_ms": 5.0,
            "overhead_method": "same_child_fresh_namespace_case_wall",
            "source_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "items": [{"line": 2, "hits": 1, "line_time_ms": 1.5,
                       "src": "    return 1"}],
        }
        with patch("optimize.line_timing.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["python"], 0, MARKER + json.dumps(base), ""
            )
            measured = profile_case(code, "f()", "f")
            self.assertTrue(measured["ok"])
            self.assertEqual(measured["profiler_overhead_ratio"], 2.5)
            bad = {**base, "items": [{**base["items"][0], "src": "    return 2"}]}
            run.return_value = subprocess.CompletedProcess(
                ["python"], 0, MARKER + json.dumps(bad), ""
            )
            self.assertEqual(profile_case(code, "f()", "f")["status"], "line_mismatch")
            no_overhead = {**base, "profiled_case_wall_ms": None}
            run.return_value = subprocess.CompletedProcess(
                ["python"], 0, MARKER + json.dumps(no_overhead), ""
            )
            self.assertEqual(profile_case(code, "f()", "f")["status"], "bad_payload")
            bad = {**base, "source_sha256": "wrong"}
            run.return_value = subprocess.CompletedProcess(
                ["python"], 0, MARKER + json.dumps(bad), ""
            )
            self.assertEqual(profile_case(code, "f()", "f")["status"], "line_mismatch")

    def test_missing_dependency_reports_reason(self):
        if HAS_LINE_PROFILER:
            self.skipTest("line_profiler is installed; missing-dependency branch not applicable")
        result = profile_case("def f():\n    return 1", "assert candidate() == 1", "f")
        self.assertEqual(result["status"], "missing_dependency")
        self.assertIn("line_profiler", result["reason"])


@unittest.skipUnless(HAS_LINE_PROFILER, "line_profiler not installed; real line timing unverified")
class RealLineProfilerTests(unittest.TestCase):
    def test_slow_and_fast_lines(self):
        code = ("import time\n"
                "def solve():\n"
                "    value = 1\n"
                "    time.sleep(0.02)\n"
                "    return value + 1\n")
        result = profile_case(code, "assert candidate() == 2", "solve")
        self.assertTrue(result["ok"], result.get("reason"))
        rows = {item["line"]: item for item in result["items"]}
        self.assertEqual(result["unit"], "ms")
        self.assertEqual(rows[4]["src"], "    time.sleep(0.02)")
        self.assertGreater(rows[4]["line_time_ms"], rows[3]["line_time_ms"] * 5)
        self.assertEqual(result["overhead_method"], "same_child_fresh_namespace_case_wall")
        self.assertGreater(result["unprofiled_case_wall_ms"], 0)
        self.assertGreater(result["profiled_case_wall_ms"], 0)
        self.assertAlmostEqual(result["profiler_overhead_ratio"],
                               result["profiled_case_wall_ms"] / result["unprofiled_case_wall_ms"])

    def test_loop_and_called_function_line_numbers(self):
        code = ("def helper(n):\n"
                "    total = 0\n"
                "    for i in range(n):\n"
                "        total += i\n"
                "    return total\n"
                "def solve(n):\n"
                "    return helper(n)\n")
        result = profile_case(code, "assert candidate(1000) == 499500", "solve")
        self.assertTrue(result["ok"], result.get("reason"))
        rows = {item["line"]: item for item in result["items"]}
        self.assertGreater(rows[4]["hits"], 1)
        self.assertIn(7, rows)
        self.assertEqual(rows[7]["src"], "    return helper(n)")

    def test_effibench_typing_list_prelude_keeps_source_lines(self):
        from eval.harness import PREAMBLE
        code = ("class Solution:\n"
                "    def run(self, values: List[int]) -> int:\n"
                "        return sum(values)\n")
        result = profile_case(code, "assert solution.run([1, 2, 3]) == 6",
                              "solution", prelude=PREAMBLE)
        self.assertTrue(result["ok"], result.get("reason"))
        rows = {item["line"]: item for item in result["items"]}
        self.assertEqual(rows[3]["src"], "        return sum(values)")
        self.assertEqual(rows[3]["hits"], 1)
        self.assertTrue(all(1 <= number <= len(code.splitlines()) for number in rows))

    def test_humaneval_40_and_55(self):
        tasks = {item["task_id"]: item for item in json.loads(DATASET.read_text(encoding="utf-8"))}
        for task_id in ("HumanEval/40", "HumanEval/55"):
            with self.subTest(task_id=task_id):
                task = tasks[task_id]
                code = task["prompt"] + task["canonical_solution"]
                from eval.harness import measure_cases
                diagnostic = measure_cases(code, task["test"], kind="humaneval",
                                           entry=task["entry_point"])
                self.assertEqual(diagnostic["status"], "ok")
                result = profile_case(code, diagnostic["slowest"]["src"],
                                      task["entry_point"], timeout_s=10)
                self.assertTrue(result["ok"], result.get("reason"))
                self.assertTrue(result["items"])
                self.assertEqual(result["unit"], "ms")


if __name__ == "__main__":
    unittest.main()
