"""Read-only integrity and pairing check for the two sealed exploratory batches.

This module never imports a model client, reads .env, runs a harness, or writes files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from optimize.prompts import render_prompt

REPO = Path(__file__).resolve().parents[2]
BANK = REPO / "results" / "bank"
SOURCE_HASHES = {
    "frozen_tasks.json": "94804c11bf488f78d989c4ddcad1f00057422125e95774279af187ad29d48371",
    "frozen_manifest.json": "a58e1b11263687f10e670a6bf6dc6d9391a90b4c8ca77a630151ce73b088f325",
}
BATCHES = {
    "gf5": {
        "directory": "gf5_2026-11-28",
        "manifest": ("frozen5_shared_manifest_2026-11-28.json",
                     "82419f2f5867b373e6fe44ad3639adc1b5e022ba9f3ed081be9a3a1c58768f87"),
        "preflight": ("frozen5_prompt_preflight_2026-11-28.json",
                      "54a052065b8687bf23470d7a60dc0e8a579cce73690c260a3f07fc25b6b953fe"),
        "trial": ("frozen5_gf_paired_trial_2026-11-28.json",
                  "75d1535b649ae5cba5ebb15092a48ce33f2ead3639023974bd75fcaa662c7714"),
        "arms": ("G0", "G1", "G2", "G3", "F0", "F1", "F2", "F3", "F4"),
        "task_count": 5,
        "calls": 45,
    },
    "m0m1": {
        "directory": "m0m1_2026-11-28",
        "manifest": ("m0m1_frozen8_manifest_2026-11-28.json",
                     "520368613083cfc8e2e8fbd788c020a402ccc85c8f294abe6dba50451d465470"),
        "preflight": ("m0m1_frozen8_preflight_2026-11-28.json",
                      "924218b50672409443e323c948b75845d059cc389a3951b938aa97c109289aab"),
        "trial": ("m0m1_frozen8_trial_2026-11-28.json",
                  "ce05ade94e0faba83ceba19c52e2767f490eb9691edf41302f494559d0a8bc18"),
        "summary": ("m0m1_frozen8_summary_2026-11-28.json",
                    "3023cf1ba6af45360cce27e59049b1a22e144878c5b4447bf5a0b69acd1745ed"),
        "arms": ("M0", "M1"),
        "task_count": 8,
        "calls": 32,
    },
}


def _pinned_json(path: Path, expected: str) -> dict | list:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("sha256_mismatch:" + str(path))
    return json.loads(raw.decode("utf-8"))


def check_batch(batch: str) -> dict:
    """Validate B bytes, every Task/input, sealed prompts and original journal."""
    if batch not in BATCHES:
        raise ValueError("unknown_batch")
    bank = _pinned_json(BANK / "frozen_tasks.json", SOURCE_HASHES["frozen_tasks.json"])
    bank_meta = _pinned_json(BANK / "frozen_manifest.json", SOURCE_HASHES["frozen_manifest.json"])
    if not isinstance(bank, list) or len(bank) != 91 or bank_meta["counts"]["frozen_selected"] != 91:
        raise ValueError("frozen_bank_count_mismatch")
    frozen_by_id = {task["task_id"]: task for task in bank}
    if len(frozen_by_id) != 91:
        raise ValueError("duplicate_frozen_task_id")
    config = BATCHES[batch]
    root = REPO / "results" / "exploratory" / config["directory"]
    docs = {name: _pinned_json(root / config[name][0], config[name][1])
            for name in ("manifest", "preflight", "trial")}
    if "summary" in config:
        docs["summary"] = _pinned_json(root / config["summary"][0], config["summary"][1])
    manifest, pre, trial = docs["manifest"], docs["preflight"], docs["trial"]
    if (len(manifest["entries"]) != config["task_count"] or
            len(pre["rows"]) != config["task_count"] or
            manifest["max_generation_calls"] != config["calls"] or
            trial["generation_calls_attempted"] != len(trial["rows"]) or
            len(trial["rows"]) != config["calls"]):
        raise ValueError("batch_size_or_budget_mismatch")
    manifest_sha = config["manifest"][1]
    preflight_sha = config["preflight"][1]
    if (pre["shared_manifest_sha256"] != manifest_sha or
            trial["shared_manifest_sha256"] != manifest_sha or
            trial["prompt_preflight_sha256"] != preflight_sha):
        raise ValueError("sealed_input_reference_mismatch")
    if (manifest["source_file_sha256"].get("CodeBoost/results/bank/frozen_tasks.json") !=
            SOURCE_HASHES["frozen_tasks.json"] or
            manifest["source_file_sha256"].get("CodeBoost/results/bank/frozen_manifest.json") !=
            SOURCE_HASHES["frozen_manifest.json"]):
        raise ValueError("bank_source_reference_mismatch")
    if (trial["formal_task"] is not False or trial["formal_result_writes_enabled"] is not False
            or any(row["formal_result_written"] for row in trial["rows"])):
        raise ValueError("unexpected_formal_result")
    entries = {entry["task_id"]: entry for entry in manifest["entries"]}
    pres = {row["task_id"]: row for row in pre["rows"]}
    if (len(entries) != config["task_count"] or len(pres) != config["task_count"]
            or set(entries) != set(pres) or not set(entries) <= set(frozen_by_id)):
        raise ValueError("task_membership_mismatch")
    for task_id, entry in entries.items():
        original = frozen_by_id[task_id]
        frozen = [x for x in original["inputs"] if x["tier"] == entry["tier"]
                  and x["args_hash"] == entry["args_hash"]]
        if (len(frozen) != 1 or frozen[0]["test"] != entry["test"] or
                original["description"] != entry["description"] or
                original["code"] != entry["initial_code"]):
            raise ValueError("frozen_task_input_mismatch:" + task_id)
        if any(hashlib.sha256(entry[field].encode("utf-8")).hexdigest() != entry[digest]
               for field, digest in (("test", "test_sha256"),
                                     ("description", "description_sha256"),
                                     ("initial_code", "initial_code_sha256"))):
            raise ValueError("entry_content_hash_mismatch:" + task_id)
        arm_input = pres[task_id]
        for arm in config["arms"]:
            values = arm_input["arms"][arm] if batch == "gf5" else arm_input[arm]
            if values["prompt"] != render_prompt(entry["description"], entry["initial_code"],
                                                  values["feedback"]):
                raise ValueError("prompt_frame_mismatch:" + task_id + ":" + arm)
        if batch == "m0m1" and not arm_input["M1"]["feedback"].startswith(
                arm_input["M0"]["feedback"]):
            raise ValueError("m1_does_not_preserve_m0")
    if batch == "gf5":
        expected_keys = {(task_id, arm) for task_id in entries for arm in config["arms"]}
        actual_keys = {(row["task_id"], row["arm"]) for row in trial["rows"]}
    else:
        expected_keys = {(task_id, arm, rep) for task_id in entries
                         for arm in config["arms"] for rep in (1, 2)}
        actual_keys = {(row["task_id"], row["arm"], row["replicate"])
                       for row in trial["rows"]}
    if (actual_keys != expected_keys or len(actual_keys) != config["calls"] or
            any((row["tier"], row["args_hash"], row["test_sha256"]) !=
                (entries[row["task_id"]]["tier"], entries[row["task_id"]]["args_hash"],
                 entries[row["task_id"]]["test_sha256"])
                for row in trial["rows"]) or
            any(row["prompt"] != (pres[row["task_id"]]["arms"][row["arm"]]["prompt"]
                                  if batch == "gf5" else pres[row["task_id"]][row["arm"]]["prompt"])
                for row in trial["rows"])):
        raise ValueError("trial_pairing_or_prompt_mismatch")
    if batch == "m0m1":
        gf_name, gf_hash = BATCHES["gf5"]["manifest"]
        gf_manifest = _pinned_json(
            REPO / "results" / "exploratory" / BATCHES["gf5"]["directory"] / gf_name,
            gf_hash)
        if set(entries) & {item["task_id"] for item in gf_manifest["entries"]}:
            raise ValueError("m0m1_overlaps_prior_five")
    return {"batch": batch, "frozen_tasks": len(bank), "selected_tasks": len(entries),
            "arms": list(config["arms"]), "calls": len(trial["rows"]),
            "formal_results_written": 0, "model_called": False,
            "source_hashes_verified": True, "archived_hashes_verified": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", choices=("gf5", "m0m1", "all"), default="all")
    args = parser.parse_args()
    batches = BATCHES if args.batch == "all" else (args.batch,)
    for batch in batches:
        print(json.dumps(check_batch(batch), ensure_ascii=False))


if __name__ == "__main__":
    main()
