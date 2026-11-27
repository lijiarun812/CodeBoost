"""Child entry point for line_profiler. Reads one JSON request from stdin."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import sys
import types

MARKER = "__CODEBOOST_LINE_TIMING__"
SOURCE_NAME = "<codeboost_source>"
CASE_NAME = "<codeboost_case>"


def _emit(result: dict) -> None:
    print(MARKER + json.dumps(result, ensure_ascii=False), flush=True)


def main() -> None:
    try:
        from line_profiler import LineProfiler
    except ImportError as exc:
        _emit({"ok": False, "status": "missing_dependency",
               "reason": f"{type(exc).__name__}: {exc}", "tool": "line_profiler"})
        return
    try:
        version = importlib.metadata.version("line_profiler")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    try:
        request = json.loads(sys.stdin.read())
        code = request["code"]
        case_src = request["case_src"]
        entry = request["entry_point"]
        namespace = {"__name__": "__codeboost_line_timing__"}
        # User code may print; keep stdout reserved for the machine-readable result.
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(code, SOURCE_NAME, "exec"), namespace)
            namespace["candidate"] = namespace.get(entry)
            profiler = LineProfiler()
            seen = set()
            for value in namespace.values():
                functions = []
                if isinstance(value, types.FunctionType):
                    functions.append(value)
                elif isinstance(value, type) and value.__module__ == namespace["__name__"]:
                    for member in vars(value).values():
                        if isinstance(member, (staticmethod, classmethod)):
                            member = member.__func__
                        if isinstance(member, types.FunctionType):
                            functions.append(member)
                for fn in functions:
                    if fn.__code__.co_filename == SOURCE_NAME and id(fn) not in seen:
                        profiler.add_function(fn)
                        seen.add(id(fn))
            if not seen:
                raise ValueError("no_user_functions_to_profile")
            with profiler:
                exec(compile(case_src, CASE_NAME, "exec"), namespace)
        stats = profiler.get_stats()
        source_lines = code.splitlines()
        merged = {}
        for (filename, _first, function), timings in stats.timings.items():
            if filename != SOURCE_NAME:
                continue
            for line, hits, ticks in timings:
                if not 1 <= line <= len(source_lines):
                    raise ValueError(f"line_out_of_range({line})")
                row = merged.setdefault(line, {"line": line, "hits": 0,
                                               "cumulative_ms": 0.0,
                                               "functions": set(),
                                               "src": source_lines[line - 1]})
                row["hits"] += int(hits)
                row["cumulative_ms"] += float(ticks) * stats.unit * 1000.0
                row["functions"].add(function)
        if not merged:
            raise ValueError("no_profiled_line_events")
        items = list(merged.values())
        for row in items:
            row["cumulative_ms"] = round(row["cumulative_ms"], 6)
            row["functions"] = sorted(row["functions"])
        items.sort(key=lambda row: (-row["cumulative_ms"], row["line"]))
        _emit({"ok": True, "status": "ok", "mode": "line_profiler",
               "tool": "line_profiler", "tool_version": version,
               "unit": "ms", "timer_unit_s": stats.unit,
               "source_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
               "items": items})
    except Exception as exc:
        _emit({"ok": False, "status": "code_error",
               "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
               "tool": "line_profiler", "tool_version": version})


if __name__ == "__main__":
    main()
