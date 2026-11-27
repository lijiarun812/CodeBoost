"""Diagnostic-only line timing in a bounded child process.

The returned times include profiler overhead. Never use them for official
speedup, Result.time, or memory metrics.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

MARKER = "__CODEBOOST_LINE_TIMING__"


def profile_case(
    code: str,
    case_src: str,
    entry_point: str = "solution",
    *,
    timeout_s: float = 10.0,
    python_executable: str | None = None,
    workdir: str | None = None,
) -> dict:
    """Profile one selected case; return explicit status, mode, units and rows."""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    payload = {"code": code, "case_src": case_src, "entry_point": entry_point}
    command = [python_executable or sys.executable,
               str(Path(__file__).with_name("_line_timing_child.py"))]
    try:
        proc = subprocess.run(
            command, input=json.dumps(payload), text=True, capture_output=True,
            timeout=timeout_s, encoding="utf-8", errors="replace", cwd=workdir,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "mode": "none",
                "reason": f"line_profiler_timeout({timeout_s:g}s)"}
    except OSError as exc:
        return {"ok": False, "status": "process_error", "mode": "none",
                "reason": f"{type(exc).__name__}: {exc}"}
    output = proc.stdout or ""
    pos = output.rfind(MARKER)
    if pos < 0:
        error = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "status": "process_error", "mode": "none",
                "reason": error[-1][:200] if error else f"exit({proc.returncode})"}
    try:
        result = json.loads(output[pos + len(MARKER):].strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        return {"ok": False, "status": "bad_payload", "mode": "none",
                "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "status": "bad_payload", "mode": "none",
                "reason": "non_object_payload"}
    if not result.get("ok"):
        return {"ok": False, "status": result.get("status", "profile_failed"),
                "mode": "none", "reason": result.get("reason", "unknown"),
                "tool": result.get("tool"), "tool_version": result.get("tool_version")}
    expected_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if result.get("source_sha256") != expected_hash:
        return {"ok": False, "status": "line_mismatch", "mode": "none",
                "reason": "source_hash_mismatch"}
    lines = code.splitlines()
    rows = result.get("items")
    if (result.get("mode") != "line_profiler" or result.get("unit") != "ms"
            or not isinstance(rows, list) or not rows):
        return {"ok": False, "status": "bad_payload", "mode": "none",
                "reason": "missing_line_profiler_rows_or_units"}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("line"), int)
                or not 1 <= row["line"] <= len(lines)
                or row.get("src") != lines[row["line"] - 1]
                or not isinstance(row.get("hits"), int)
                or not isinstance(row.get("cumulative_ms"), (int, float))
                or row["hits"] < 0 or row["cumulative_ms"] < 0):
            return {"ok": False, "status": "line_mismatch", "mode": "none",
                    "reason": "line_number_or_source_mismatch"}
    return result
