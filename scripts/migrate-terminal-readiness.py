#!/usr/bin/env python3
"""Carry verified preflight tickets across a Docker storage-driver cutover.

Only the disk gate in the preflight controller changed. Existing smoke evidence
is retained; a ticket is refreshed only when its image ID matches on overlay2.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.terminal_infra import (  # noqa: E402
    DATASET,
    atomic_json,
    fingerprint,
    fingerprint_paths,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--docker-host", default="unix:///tmp/rsi-harness-docker-overlay2/docker.sock")
    args = parser.parse_args()
    root = args.root.resolve()
    docker = "/home/ytzhang/budget-rl/.deps/docker-static/docker"
    info = subprocess.run(
        [docker, "-H", args.docker_host, "info", "--format", "{{.Driver}}"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    if info.stdout.strip() != "overlay2":
        parser.error("destination Docker is not overlay2")
    updated = skipped = 0
    for path in sorted((root / "_infra/readiness").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        task = DATASET / path.stem
        if record.get("status") != "ready" or record.get("task") != path.stem or not task.is_dir():
            skipped += 1
            continue
        tests = record.get("tests") or {}
        runtime = record.get("runtime") or {}
        if (
            runtime.get("tmux") != "passed"
            or not isinstance(tests.get("tests"), int)
            or tests["tests"] <= 0
            or tests.get("other", 0)
            or tests.get("pending", 0)
            or runtime.get("image") != record.get("image")
        ):
            raise RuntimeError(f"incomplete smoke evidence: {path}")
        # Do not bless a ticket made stale by a changed task, verifier, or
        # runtime. The sole allowed post-smoke edit is this controller's disk
        # gate; its changes do not affect the smoke's semantic result.
        controller = PROJECT / "scripts/preflight-terminal-environments.py"
        changed_inputs = [
            str(input_path)
            for input_path in fingerprint_paths(task)
            if input_path != controller
            and input_path.stat().st_mtime > float(record["finished"])
        ]
        if changed_inputs:
            raise RuntimeError(f"other preflight inputs changed after smoke: {path}: {changed_inputs}")
        result = subprocess.run(
            [docker, "-H", args.docker_host, "image", "inspect", record["image"], "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode or result.stdout.strip() != record.get("image_id"):
            raise RuntimeError(f"preflight image ID mismatch: {path}")
        current = fingerprint(task)
        if record.get("fingerprint") == current:
            skipped += 1
            continue
        record["fingerprint_migration"] = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "from": record["fingerprint"],
            "to": current,
            "reason": "Docker VFS-to-overlay2 cutover; only preflight disk gate changed",
        }
        record["fingerprint"] = current
        atomic_json(path, record)
        updated += 1
    print(f"Readiness tickets verified: updated={updated} unchanged_or_nonready={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
