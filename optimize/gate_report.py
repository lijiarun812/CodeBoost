"""Evidence labels for offline F4 gate reports; never infer missing RSS values."""

from __future__ import annotations

from typing import Mapping


def rss_evidence(result: Mapping, *, psutil_available: bool,
                 python_executable: str) -> dict:
    """Classify an RSS peak using the interpreter that ran the harness.

    A schema Result has no memory-round status or RSS sampling diagnostics.
    Consequently a null peak with psutil installed has an unknown cause.
    """
    peak = (result.get("mem") or {}).get("peak_rss_mb")
    if peak is not None:
        status = "measured"
        reason = None
    elif not psutil_available:
        status = "psutil_unavailable"
        reason = "psutil_import_unavailable_in_harness_python"
    else:
        status = "rss_not_obtained"
        reason = "psutil_available_but_rss_missing_cause_unknown"
    return {
        "python_executable": python_executable,
        "psutil_status": "available" if psutil_available else "unavailable",
        "rss_status": status,
        "reason": reason,
        "peak_rss_mb": peak,
        "memory_round_status": "unknown",
        "missing_harness_fields": [
            "memory_round_status", "memory_round_error",
            "rss_sampler_status", "rss_sample_count",
        ],
    }
