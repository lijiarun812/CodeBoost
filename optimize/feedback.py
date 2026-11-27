"""Build F0–F4 prompt feedback without changing the shared eval protocol.

build_feedback() returns a JSON-friendly dict with prompt, feedback,
schema projection and diagnostics. A future runner owns model calls,
official Result measurement, and persistence. total_ms must come from
the runner's official whole-task measurement; per-case totals are never used
as a substitute.
"""

from __future__ import annotations

import hashlib
import math

from eval import harness
from eval.hotlines import hotspots

from optimize.line_timing import profile_case
from optimize.profiler import context_regions, scopes_at
from optimize.prompts import render_prompt

_METHODS = {"f0": "vanilla", "f1": "f1", "f2": "f2", "f3": "f3", "f4": "ours"}
_NO_FEEDBACK = "(No performance feedback available.)"
_MAX_DIAGNOSTIC_TIMEOUT_S = 60.0


def _bounded(lines: list[str], max_chars: int) -> tuple[str, bool, int]:
    """Keep only complete lines within the character budget."""
    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    output = []
    length = 0
    for line in lines:
        added = len(line) + (1 if output else 0)
        if length + added + 16 > max_chars:
            marker = "... [truncated]"
            kept_count = len(output)
            if length + len(marker) + 1 <= max_chars:
                output.append(marker)
            return "\n".join(output), True, kept_count
        output.append(line)
        length += added
    return "\n".join(output), False, len(output)


def _line_ranges(source_line_numbers: set[int]) -> list[tuple[int, int]]:
    ranges = []
    for number in sorted(source_line_numbers):
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], number)
        else:
            ranges.append((number, number))
    return ranges


def _retained_hotspot_context(source_ranges: list[tuple[int, int]], items: list[dict],
                              source_line_count: int) -> list[int]:
    """Use emitted source-line ranges, never repeated source-text substrings."""
    def covered(number: int) -> bool:
        return any(start <= number <= end for start, end in source_ranges)

    retained = []
    for item in items:
        line = item["line"]
        if not covered(line):
            continue
        nearby = [n for n in (line - 1, line + 1) if 1 <= n <= source_line_count]
        if not nearby or any(covered(n) for n in nearby):
            retained.append(line)
    return retained


def _timing_source_error(timing: dict, code: str) -> str | None:
    """Reject stale or malformed injected timing just like subprocess results."""
    lines = code.splitlines()
    if timing.get("source_sha256") != hashlib.sha256(code.encode("utf-8")).hexdigest():
        return "source_hash_mismatch"
    if timing.get("mode") != "line_profiler" or timing.get("unit") != "ms":
        return "mode_or_unit_mismatch"
    rows = timing.get("items")
    if not isinstance(rows, list) or not rows:
        return "no_line_timing_rows"
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("line"), int)
                or not 1 <= row["line"] <= len(lines)
                or row.get("src") != lines[row["line"] - 1]
                or not isinstance(row.get("hits"), int)
                or not isinstance(row.get("line_time_ms"), (int, float))
                or not math.isfinite(row["line_time_ms"])
                or row["line_time_ms"] < 0):
            return "line_number_or_source_mismatch"
    if sum(row["line_time_ms"] for row in rows) <= 0:
        return "zero_profiled_time"
    return None


def build_feedback(
    method: str,
    task: dict,
    code: str,
    *,
    total_ms: float | None = None,
    diagnostic: dict | None = None,
    hot: dict | None = None,
    line_timing: dict | None = None,
    line_timeout_s: float = 10.0,
    radius: int = 8,
    max_chars: int = 4800,
    workdir: str | None = None,
) -> dict:
    """Construct a feedback arm; optional diagnostic/hot inputs permit reuse.

    F2–F4 select the slowest case through harness.measure_cases(). F3 uses
    line execution counts. F4 first attempts diagnostic-only line_profiler
    timing, falling back to line_hits if it fails. If both fail, it degrades
    to F2; if per-case diagnosis fails, it degrades to F1
    when an official total_ms exists, otherwise to a neutral placeholder.
    The complete description is Task.description from schema v1.1. For a
    future Result, method is the actual schema method and requested_method
    is the assigned method. A pending schema projection must not be written
    as a formal Result or counted in ablation comparisons.
    """
    arm = method.lower()
    if arm not in _METHODS:
        raise ValueError("method must be one of f0, f1, f2, f3, f4")
    if radius < 0:
        raise ValueError("radius must be nonnegative")
    if not math.isfinite(line_timeout_s) or not 0 < line_timeout_s <= _MAX_DIAGNOSTIC_TIMEOUT_S:
        raise ValueError("line_timeout_s must be positive and at most 60 seconds")
    description = task.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Task.description is required (full problem description)")
    meta = {"requested": arm, "effective": arm, "reasons": [], "case": None,
            "case_mode": None, "case_count": None, "slow_ms": None,
            "diagnostic_timeout_s": None,
            "diagnostic_timeout_cap_s": _MAX_DIAGNOSTIC_TIMEOUT_S,
            "hotspot_mode": None,
            "line_timing": None, "truncated": False}
    feedback = _NO_FEEDBACK

    if arm == "f0":
        feedback = "(No performance measurements provided.)"
    elif arm == "f1":
        feedback = (
            f"Whole-task execution time: {total_ms:.4f} ms."
            if total_ms is not None else _NO_FEEDBACK
        )
        if total_ms is None:
            meta["effective"] = "none"
            meta["reasons"].append("official_total_ms_missing")
    else:
        try:
            diag = diagnostic if diagnostic is not None else harness.measure_cases(
                code, task["test"],
                kind=task.get("kind", "humaneval" if task.get("dataset") == "humaneval" else "assert"),
                entry=task.get("entry_point", "solution"), workdir=workdir,
            )
        except Exception as exc:  # diagnostic must not break prompt generation
            diag = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        diag = diag if isinstance(diag, dict) else {"status": "error", "error": "invalid_case_payload"}
        meta["case_mode"] = diag.get("mode")
        cases = diag.get("cases")
        meta["case_count"] = len(cases) if isinstance(cases, list) else None
        slow = diag.get("slowest") or {}
        slow = slow if isinstance(slow, dict) else {}
        if (diag.get("status") != "ok" or not isinstance(slow.get("src"), str)
                or not slow.get("src") or isinstance(slow.get("ms"), bool)
                or not isinstance(slow.get("ms"), (int, float))
                or not math.isfinite(slow["ms"]) or slow["ms"] < 0):
            meta["effective"] = "f1" if total_ms is not None else "none"
            meta["reasons"].append(
                "case_diagnostic_failed: " + str(diag.get("error") or diag.get("status") or "empty_case")
            )
            if total_ms is not None:
                feedback = f"Whole-task execution time: {total_ms:.4f} ms."
        else:
            meta["case"] = {"idx": slow.get("idx"), "ms": slow.get("ms"),
                            "src": slow["src"]}
            meta["slow_ms"] = slow["ms"]
            feedback = f"Slowest test case:\n{slow['src']}\nCase execution time: {slow['ms']:.4f} ms."
            if arm in ("f3", "f4"):
                timeout_s = min(_MAX_DIAGNOSTIC_TIMEOUT_S,
                                max(line_timeout_s, 20.0 * slow["ms"] / 1000.0))
                meta["diagnostic_timeout_s"] = timeout_s
                timing = None
                if arm == "f4":
                    if slow["ms"] < 2.0:
                        timing = {"ok": False, "status": "case_too_small",
                                  "reason": "case_too_small(<2ms)"}
                    else:
                        try:
                            timing = line_timing if line_timing is not None else profile_case(
                                code, slow["src"], task.get("entry_point", "solution"),
                                timeout_s=timeout_s, workdir=workdir,
                                prelude=harness.PREAMBLE if task.get("kind") == "test_block" else "",
                            )
                        except Exception as exc:
                            timing = {"ok": False, "status": "process_error",
                                      "reason": f"{type(exc).__name__}: {exc}"}
                    timing = timing if isinstance(timing, dict) else {
                        "ok": False, "status": "bad_payload", "reason": "invalid_line_timing_payload"
                    }
                    if timing.get("ok"):
                        source_error = _timing_source_error(timing, code)
                        if source_error:
                            timing = {"ok": False, "status": "line_mismatch",
                                      "reason": source_error, "tool": timing.get("tool"),
                                      "tool_version": timing.get("tool_version")}
                    meta["line_timing"] = {
                        "status": timing.get("status"), "mode": timing.get("mode"),
                        "tool": timing.get("tool"), "tool_version": timing.get("tool_version"),
                        "reason": timing.get("reason"), "unit": timing.get("unit"),
                    }
                    if not timing.get("ok"):
                        meta["reasons"].append(
                            "line_timing_failed: " + str(timing.get("reason") or timing.get("status"))
                        )
                use_timing = bool(arm == "f4" and timing and timing.get("ok"))
                # The existing line_hits route is the explicit fallback, not a timing estimate.
                if use_timing:
                    hits = None
                else:
                    try:
                        hits = hot if hot is not None else hotspots(
                            code, slow["src"], task.get("entry_point", "solution"),
                            top=3, max_time=timeout_s, workdir=workdir,
                            prelude=harness.PREAMBLE if task.get("kind") == "test_block" else "",
                        )
                    except Exception as exc:
                        hits = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                    hits = hits if isinstance(hits, dict) else {
                        "ok": False, "reason": "invalid_hotspot_payload"
                    }
                source_lines = code.splitlines()
                source_rows = timing.get("items") if use_timing else hits.get("items")
                items = [
                    item for item in (source_rows or [])
                    if isinstance(item, dict) and isinstance(item.get("line"), int)
                    and isinstance(item.get("hits"), int)
                    and 1 <= item["line"] <= len(source_lines)
                    and (not use_timing or (
                        isinstance(item.get("line_time_ms"), (int, float))
                        and item.get("src") == source_lines[item["line"] - 1]
                    ))
                ][:3]
                signal_failed = (not items or (not use_timing and (
                    not hits.get("ok") or hits.get("error")
                )))
                if signal_failed:
                    meta["effective"] = "f2"
                    meta["reasons"].append(
                        "line_signal_failed: " + str(
                            (hits or {}).get("reason") or (hits or {}).get("error")
                            or "no_valid_lines"
                        )
                    )
                else:
                    meta["hotspot_mode"] = "line_profiler" if use_timing else "line_hits"
                    meta["hot_lines"] = [item["line"] for item in items]
                    profiled_total_ms = (sum(row["line_time_ms"] for row in timing["items"])
                                         if use_timing else None)
                    if use_timing:
                        feedback = "热点行占本次已剖析行时间之和的比例（line_profiler 诊断值，含剖析开销；不用于最终 speedup；不含被调函数耗时）:\n"
                        feedback += "\n".join(
                            f"L{item['line']}: {item['hits']} hits | "
                            f"占本次已剖析行时间之和的比例 {100 * item['line_time_ms'] / profiled_total_ms:.1f}% | "
                            f"{source_lines[item['line'] - 1].rstrip()}"
                            for item in items
                        )
                    else:
                        if arm == "f4":
                            meta["effective"] = "f4_line_hits"
                        feedback = "高频执行行（按执行次数排序，非逐行耗时）:\n"
                        feedback += "\n".join(
                            f"L{item['line']}: {item['hits']} hits | "
                            f"{source_lines[item['line'] - 1].rstrip()}"
                            for item in items
                        )
                    if arm == "f4":
                        try:
                            regions = context_regions(code, meta["hot_lines"], radius)
                            region_lines = [
                                ("热点行占本次已剖析行时间之和的比例与周围源码（line_profiler 诊断值，"
                                 "含剖析开销；不用于最终 speedup；不含被调函数耗时）:"
                                 if use_timing else
                                 "高频执行行与周围源码（按执行次数排序，非逐行耗时）:")
                            ]
                            for item in items:
                                summary = f"L{item['line']}: {item['hits']} hits"
                                if use_timing:
                                    summary += (" | 占本次已剖析行时间之和的比例 "
                                                f"{100 * item['line_time_ms'] / profiled_total_ms:.1f}%")
                                region_lines.append(summary)
                                scope = scopes_at(code, item["line"])
                                region_lines.append(
                                    "  Enclosing function/loop: "
                                    + (" -> ".join(scope) if scope else "(module)")
                                )
                            source_line_positions = {}
                            for region in regions:
                                region_lines.append(
                                    f"Source lines {region['start']}-{region['end']} "
                                    f"(hot lines: {', '.join(map(str, region['hits']))}):"
                                )
                                for number in range(region["start"], region["end"] + 1):
                                    source_line_positions[len(region_lines)] = number
                                    region_lines.append(f"{number:>4}: {source_lines[number - 1]}")
                            feedback, meta["truncated"], kept_count = _bounded(region_lines, max_chars)
                            meta["regions"] = [
                                {"start": region["start"], "end": region["end"],
                                 "hits": region["hits"]} for region in regions
                            ]
                            retained_ranges = _line_ranges({number for index, number
                                                            in source_line_positions.items()
                                                            if index < kept_count})
                            meta["retained_source_ranges"] = retained_ranges
                            meta["retained_hot_lines"] = _retained_hotspot_context(
                                retained_ranges, items, len(source_lines)
                            )
                            if not meta["retained_hot_lines"]:
                                if use_timing:
                                    try:
                                        fallback_hits = hot if hot is not None else hotspots(
                                            code, slow["src"], task.get("entry_point", "solution"),
                                            top=3, max_time=timeout_s, workdir=workdir,
                                            prelude=harness.PREAMBLE if task.get("kind") == "test_block" else "",
                                        )
                                    except Exception as exc:
                                        fallback_hits = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                                    fallback_items = (fallback_hits.get("items") or []) if isinstance(fallback_hits, dict) else []
                                    fallback_items = [item for item in fallback_items
                                                      if isinstance(item, dict)
                                                      and isinstance(item.get("line"), int)
                                                      and isinstance(item.get("hits"), int)
                                                      and 1 <= item["line"] <= len(source_lines)][:3]
                                else:
                                    fallback_items = items
                                if not fallback_items:
                                    meta["effective"] = "f2"
                                    meta["hotspot_mode"] = None
                                    meta["reasons"].append("line_hits_fallback_failed")
                                    feedback = (f"Slowest test case:\n{slow['src']}\n"
                                                f"Case execution time: {slow['ms']:.4f} ms.")
                                else:
                                    meta["effective"] = "f3"
                                    meta["hotspot_mode"] = "line_hits"
                                    meta["hot_lines"] = [item["line"] for item in fallback_items]
                                    meta["reasons"].append("f4_context_omitted_by_length_limit")
                                    feedback = "高频执行行（按执行次数排序，非逐行耗时）:\n"
                                    feedback += "\n".join(
                                        f"L{item['line']}: {item['hits']} hits | "
                                        f"{source_lines[item['line'] - 1].rstrip()}"
                                        for item in fallback_items
                                    )
                        except (SyntaxError, ValueError) as exc:
                            meta["effective"] = "f2"
                            meta["hotspot_mode"] = None
                            meta["reasons"].append(
                                f"source_context_failed: {type(exc).__name__}: {exc}"
                            )
                            feedback = (f"Slowest test case:\n{slow['src']}\n"
                                        f"Case execution time: {slow['ms']:.4f} ms.")

    effective = meta["effective"]
    # schema v1.1 has no f4_line_hits method. Full F4 also still lacks space
    # feedback. Keep both out of formal Results until the group fixes a policy.
    mapping_pending = (arm == "f4" and effective in ("f4", "f4_line_hits"))
    if effective == arm:
        degrade_reason = None
    elif effective == "f3" and "f4_context_omitted_by_length_limit" in meta["reasons"]:
        degrade_reason = "budget_exceeded"
    elif effective == "f2":
        degrade_reason = "fallback_f2"
    elif effective in ("f1", "f3", "f4_line_hits"):
        degrade_reason = "no_hotspot"
    else:
        degrade_reason = "no_hotspot"
        mapping_pending = True
    actual_method = None if mapping_pending else _METHODS.get(effective)
    eligible = not mapping_pending and effective == arm and degrade_reason is None
    return {
        "method": actual_method,
        "requested_method": _METHODS[arm],
        "degrade_reason": degrade_reason,
        "schema_mapping_pending": mapping_pending,
        "can_write_result": not mapping_pending,
        "requested_arm": arm,
        "effective_arm": effective,
        "effective_method": effective,
        "eligible_for_requested_method": eligible,
        "feedback": feedback,
        "prompt": render_prompt(description, code, feedback),
        "diagnostics": meta,
    }
