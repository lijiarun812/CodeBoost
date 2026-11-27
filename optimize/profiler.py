"""Source-context helpers. Line hits are counts, never per-line timings."""

from __future__ import annotations

import ast


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.For, ast.AsyncFor, ast.While)


def scopes_at(code: str, line: int) -> list[str]:
    """Return enclosing function and loop headers, outermost first."""
    tree = ast.parse(code)
    lines = code.splitlines()
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, _SCOPES):
            continue
        if node.lineno <= line <= node.end_lineno:
            header = lines[node.lineno - 1].rstrip()
            matches.append((node.lineno, -(node.end_lineno - node.lineno), header))
    return [header for _, _, header in sorted(matches)]


def context_regions(code: str, hit_lines: list[int], radius: int = 8) -> list[dict]:
    """Merge overlapping source windows while retaining original line numbers/text."""
    lines = code.splitlines()
    windows = []
    for line in sorted(set(hit_lines)):
        if not 1 <= line <= len(lines):
            continue
        windows.append((max(1, line - radius), min(len(lines), line + radius), line))
    regions = []
    for start, end, hit in windows:
        if regions and start <= regions[-1]["end"] + 1:
            regions[-1]["end"] = max(end, regions[-1]["end"])
            regions[-1]["hits"].append(hit)
        else:
            regions.append({"start": start, "end": end, "hits": [hit]})
    for region in regions:
        region["source"] = "".join(
            f"{number:>4}: {lines[number - 1]}\n"
            for number in range(region["start"], region["end"] + 1)
        ).rstrip("\n")
    return regions
