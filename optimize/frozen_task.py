"""Read-only check against independently pinned B task-bank artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from eval.harness import PROTOCOL_VERSION


def _read_json(path: str | Path) -> tuple[dict | list, str]:
    data = Path(path).read_bytes()
    return json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()


def _provisional_ids(path: str | Path | None) -> set[str]:
    if path is None:
        return set()
    rows, _ = _read_json(path)
    if not isinstance(rows, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("task_id"), str)
            for item in rows):
        raise ValueError("provisional_list_shape_invalid")
    return {item["task_id"] for item in rows}


def _valid_sha(value: str | None) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"(?:sha256:)?[0-9a-fA-F]{64}", value) is not None


def verify_frozen_task(task: Mapping[str, Any], frozen_input: Mapping[str, Any],
                       *, manifest_path: str | Path | None,
                       approved_sha256: str | None,
                       bank_manifest_path: str | Path | None = None,
                       approved_bank_manifest_sha256: str | None = None,
                       provisional_list_path: str | Path | None = None) -> dict:
    """Require pinned bytes and an exact task plus selected input match.

    Accept diagnostic JSON fixtures/JSONL or B's frozen JSON Task array.
    The frozen array requires independently approved hashes of both files.
    """
    record = {"verified": False, "content_matched": False,
              "provisional": False, "reason": "frozen_manifest_unavailable",
              "manifest_sha256": None, "bank_manifest_sha256": None,
              "summary_consistent": None, "task_id": task.get("task_id"),
              "tier": frozen_input.get("tier"), "args_hash": frozen_input.get("args_hash")}
    if manifest_path is None:
        return record
    if approved_sha256 is not None and not _valid_sha(approved_sha256):
        record["reason"] = "approved_manifest_hash_invalid"
        return record
    try:
        data = Path(manifest_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        record["manifest_sha256"] = digest
        if approved_sha256 is not None and digest != approved_sha256.removeprefix("sha256:"):
            record["reason"] = "frozen_manifest_hash_mismatch"
            return record
        is_jsonl = Path(manifest_path).suffix.lower() == ".jsonl"
        if is_jsonl:
            entries = [json.loads(line) for line in data.decode("utf-8").splitlines()
                       if line.strip()]
            format_name = "jsonl"
        else:
            document = json.loads(data.decode("utf-8"))
            entries = (document if isinstance(document, list) else
                       document.get("tasks") if isinstance(document, dict) else None)
            format_name = "frozen_json" if isinstance(document, list) else "json_tasks"
        record["format"] = format_name
    except (OSError, UnicodeError, ValueError) as exc:
        record["reason"] = f"frozen_manifest_unreadable:{type(exc).__name__}"
        return record
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
    record["content_matched"] = True
    if format_name == "json_tasks":
        # The old JSON fixture has no B freeze metadata.
        record["reason"] = "legacy_json_fixture_diagnostic_only"
        return record
    try:
        provisional = _provisional_ids(provisional_list_path)
        if bank_manifest_path is None:
            record["reason"] = "bank_manifest_unavailable"
            record["provisional"] = task.get("task_id") in provisional
            return record
        summary, summary_sha = _read_json(bank_manifest_path)
        record["bank_manifest_sha256"] = summary_sha
    except (OSError, UnicodeError, ValueError) as exc:
        record["reason"] = f"bank_metadata_unreadable:{type(exc).__name__}"
        return record
    if not isinstance(summary, dict):
        record["reason"] = "bank_manifest_shape_invalid"
        return record
    counts = summary.get("counts") or {}
    if format_name == "frozen_json":
        record["summary_consistent"] = bool(
            isinstance(counts, dict) and counts.get("frozen_selected") == len(entries)
            and summary.get("protocol_version") == PROTOCOL_VERSION
            and isinstance(summary.get("frozen_at"), str)
            and isinstance(summary.get("selection_criteria"), dict))
        if not record["summary_consistent"]:
            record["reason"] = "bank_manifest_summary_mismatch"
        elif record["provisional"]:
            record["reason"] = "provisional_task"
        elif approved_sha256 is None or approved_bank_manifest_sha256 is None:
            record["reason"] = "frozen_digests_unapproved"
        elif not _valid_sha(approved_bank_manifest_sha256):
            record["reason"] = "approved_bank_manifest_hash_invalid"
        elif summary_sha != approved_bank_manifest_sha256.removeprefix("sha256:"):
            record["reason"] = "bank_manifest_hash_mismatch"
        else:
            record.update({"verified": True,
                           "reason": "matched_pinned_b_frozen_json_and_manifest"})
        return record
    output_name = str(summary.get("output") or "").replace("\\", "/").split("/")[-1]
    record["summary_consistent"] = bool(
        isinstance(counts, dict) and counts.get("valid_tasks") == len(entries)
        and summary.get("dataset") == task.get("dataset")
        and summary.get("protocol_version") == PROTOCOL_VERSION
        and output_name == Path(manifest_path).name)
    record["provisional"] = task.get("task_id") in provisional
    if not record["summary_consistent"]:
        record["reason"] = "bank_manifest_summary_mismatch"
    elif record["provisional"]:
        record["reason"] = "provisional_task"
    elif summary.get("status") != "frozen":
        record["reason"] = "bank_not_frozen"
    elif summary.get("task_bank_sha256") != digest:
        record["reason"] = "bank_digest_missing_or_mismatch"
    elif (not isinstance(summary.get("frozen_task_ids"), list) or
          task.get("task_id") not in summary["frozen_task_ids"]):
        record["reason"] = "task_not_in_frozen_subset"
    elif approved_sha256 is None or approved_bank_manifest_sha256 is None:
        record["reason"] = "frozen_digests_unapproved"
    elif (not isinstance(approved_bank_manifest_sha256, str) or
          summary_sha != approved_bank_manifest_sha256.removeprefix("sha256:")):
        record["reason"] = "bank_manifest_hash_mismatch"
    else:
        record.update({"verified": True, "reason": "matched_pinned_b_jsonl_and_frozen_manifest"})
    return record
