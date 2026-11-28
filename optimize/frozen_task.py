"""Read-only check against an independently pinned B task-list artifact.

This is an integration contract, not a claim that B has published a frozen
list. Until its path, shape, and SHA-256 are agreed, verification is false.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


def verify_frozen_task(task: Mapping[str, Any], frozen_input: Mapping[str, Any],
                       *, manifest_path: str | Path | None,
                       approved_sha256: str | None) -> dict:
    """Require pinned bytes and an exact task plus selected input match.

    Proposed B file shape: ``{"tasks": [schema-shaped Task, ...]}``. The
    approved digest must come from an independent team decision, never be
    computed by the caller from the file immediately before this check.
    """
    record = {"verified": False, "reason": "frozen_manifest_unavailable",
              "manifest_sha256": None, "task_id": task.get("task_id"),
              "tier": frozen_input.get("tier"), "args_hash": frozen_input.get("args_hash")}
    if manifest_path is None or approved_sha256 is None:
        return record
    if not isinstance(approved_sha256, str) or not re.fullmatch(
            r"(?:sha256:)?[0-9a-fA-F]{64}", approved_sha256):
        record["reason"] = "approved_manifest_hash_invalid"
        return record
    try:
        data = Path(manifest_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        record["manifest_sha256"] = digest
        if digest != approved_sha256.removeprefix("sha256:"):
            record["reason"] = "frozen_manifest_hash_mismatch"
            return record
        document = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        record["reason"] = f"frozen_manifest_unreadable:{type(exc).__name__}"
        return record
    entries = document.get("tasks") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        record["reason"] = "frozen_manifest_shape_invalid"
        return record
    matches = [entry for entry in entries if isinstance(entry, dict)
               and entry.get("task_id") == task.get("task_id")]
    if len(matches) != 1:
        record["reason"] = "frozen_task_not_unique_or_missing"
        return record
    entry = matches[0]
    fields = ("task_id", "dataset", "kind", "entry_point", "description", "code", "test")
    if any(not isinstance(entry.get(key), str) or entry[key] != task.get(key)
           for key in fields):
        record["reason"] = "frozen_task_content_mismatch"
        return record
    inputs = entry.get("inputs")
    if not isinstance(inputs, list):
        record["reason"] = "frozen_manifest_shape_invalid"
        return record
    selected = [item for item in inputs if isinstance(item, dict)
                and item.get("tier") == frozen_input.get("tier")
                and item.get("args_hash") == frozen_input.get("args_hash")]
    if len(selected) != 1 or not all(isinstance(frozen_input.get(key), str)
                                     and frozen_input.get(key) for key in ("tier", "args_hash")):
        record["reason"] = "frozen_tier_hash_not_unique_or_missing"
        return record
    selected_test = selected[0].get("test", entry.get("test"))
    local_test = frozen_input.get("test", task.get("test"))
    if selected_test != local_test:
        record["reason"] = "frozen_input_test_mismatch"
        return record
    if any(selected[0].get(key) != frozen_input.get(key) for key in ("n", "gen")):
        record["reason"] = "frozen_input_metadata_mismatch"
        return record
    record.update({"verified": True, "reason": "matched_pinned_b_task_and_input"})
    return record
