#!/usr/bin/env python3
"""Prepare and smoke-test environments without using models or formal results.

Each unchanged task is attempted once. Failed preflights are quarantined until
an explicit --retry-failed after diagnosis; waiting for an image is not failure.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.terminal_infra import (
    DATASET,
    atomic_json,
    docker_disk_status,
    fingerprint,
    readiness,
)


def inspect_image(image: str) -> str | None:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_report(trial: Path) -> dict:
    result = json.loads((trial / "result.json").read_text())
    if result.get("exception_info"):
        raise RuntimeError(json.dumps(result["exception_info"])[:3000])
    report = json.loads((trial / "verifier/ctrf.json").read_text())
    summary = report["results"]["summary"]
    count = summary.get("tests")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise RuntimeError(f"verifier did not execute tests: {summary}")
    # Test assertions may fail on an unsolved environment. Collection/bootstrap
    # failures are NOT accepted as a ready verifier.
    if summary.get("other", 0) or summary.get("pending", 0):
        raise RuntimeError(f"verifier has collection/runtime errors: {summary}")
    runtime = json.loads((trial / "agent/infrastructure.json").read_text())
    if runtime.get("tmux") != "passed":
        raise RuntimeError("missing tmux smoke evidence")
    return {"tests": summary, "runtime": runtime}


def preflight(root: Path, task: Path) -> dict:
    target = root / "_infra/readiness" / f"{task.name}.json"
    record = {
        "task": task.name,
        "fingerprint": fingerprint(task),
        "started": time.time(),
        "status": "running",
    }
    atomic_json(target, record)
    output = root / "_infra/trials" / task.name / str(time.time_ns())
    output.mkdir(parents=True)
    record["output"] = str(output)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(
        HARBOR_FORMAL_REQUIRE_CLEAN_VERIFIER="1",
        HARBOR_TMUX_BUNDLE=str(
            PROJECT / ".cache/tmux/tmux-runtime-singleuid-amd64.tar.gz"
        ),
        HARBOR_VERIFIER_BUNDLE=str(
            PROJECT / ".cache/tmux/verifier-runtime-singleuid-amd64.tar.gz"
        ),
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    command = [
        str(PROJECT / ".venv-terminal/bin/harbor"),
        "trials",
        "start",
        "--path",
        str(task),
        "--trials-dir",
        str(output),
        "--agent-import-path",
        "rsi_harness.terminal_smoke_agent:TerminalSmokeAgent",
        "--environment-build-timeout-multiplier",
        "4",
        "--verifier-timeout",
        "600",
    ]
    for key, value in {
        "HTTP_PROXY": "http://10.0.2.2:17897",
        "HTTPS_PROXY": "http://10.0.2.2:17897",
        "http_proxy": "http://10.0.2.2:17897",
        "https_proxy": "http://10.0.2.2:17897",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "TAR_OPTIONS": "--no-same-owner",
        "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "UV_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
    }.items():
        command += ["--ve", f"{key}={value}"]
    try:
        with (output / "preflight.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command,
                cwd=PROJECT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            record["pid"] = process.pid
            atomic_json(target, record)
            try:
                process.wait(timeout=4200)
            except subprocess.TimeoutExpired:
                # Terminate only this isolated preflight group, not formal workers.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                raise RuntimeError(
                    "preflight exceeded 4200 seconds; inspect container cleanup"
                )
        trials = list(output.glob("*/result.json"))
        if len(trials) != 1:
            raise RuntimeError(
                f"expected one preflight result, found {len(trials)}; exit={process.returncode}"
            )
        evidence = test_report(trials[0].parent)
        image = evidence["runtime"]["image"]
        image_id = inspect_image(image)
        if not image_id:
            raise RuntimeError(f"preflight image disappeared: {image}")
        record.update(status="ready", image=image, image_id=image_id, **evidence)
    except Exception as exc:  # noqa: BLE001 - quarantine failures; do not lose the queue
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    record["finished"] = time.time()
    atomic_json(target, record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def inputs_available(task: Path) -> bool:
    config = (task / "task.toml").read_text()
    match = re.search(r'^\s*docker_image\s*=\s*"([^"]+)"', config, re.MULTILINE)
    if match:
        return bool(inspect_image(match[1]))
    # Never start a formal/preflight build which silently downloads an
    # unnormalised base on this single-UID daemon.
    dockerfile = (task / "environment/Dockerfile").read_text()
    bases = re.findall(
        r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE
    )
    aliases = set(
        re.findall(
            r"^FROM\s+.*?\s+AS\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE
        )
    )
    return all(
        base == "scratch" or base in aliases or inspect_image(base) for base in bases
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--task", action="append")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    infra = root / "_infra"
    infra.mkdir(parents=True, exist_ok=True)
    lock = (infra / "preflight.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    tasks = (
        [DATASET / name for name in args.task]
        if args.task
        else sorted(p.parent for p in DATASET.glob("*/task.toml"))
    )
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    attempted = set()
    active = {}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        while not stopping or active:
            for name, future in list(active.items()):
                if future.done():
                    future.result()
                    del active[name]
            disk = docker_disk_status(PROJECT)
            disk_ok = disk["ok"]
            waiting = []
            if not stopping and disk_ok:
                for task in tasks:
                    if len(active) >= max(1, args.jobs):
                        break
                    attempt_key = (task.name, fingerprint(task))
                    if task.name in active or attempt_key in attempted:
                        continue
                    record = readiness(root, task.name)
                    if record["status"] == "ready" or (
                        record["status"] == "failed" and not args.retry_failed
                    ):
                        continue
                    # A stale running record is quarantined, not blindly duplicated.
                    if record["status"] == "running":
                        waiting.append(
                            {
                                "task": task.name,
                                "reason": "previous_preflight_running_or_interrupted",
                            }
                        )
                        continue
                    try:
                        available = inputs_available(task)
                    except (OSError, subprocess.TimeoutExpired):
                        available = False
                    if not available:
                        waiting.append(
                            {
                                "task": task.name,
                                "reason": "base_or_prebuilt_image_missing",
                            }
                        )
                        continue
                    attempted.add(attempt_key)
                    active[task.name] = pool.submit(preflight, root, task)
            statuses = {}
            for task in tasks:
                status = readiness(root, task.name)["status"]
                statuses[status] = statuses.get(status, 0) + 1
            atomic_json(
                infra / "status.json",
                {
                    "pid": os.getpid(),
                    "time": time.time(),
                    "active": list(active),
                    "counts": statuses,
                    "waiting": waiting,
                    "disk_ok": disk_ok,
                    "disk": disk,
                    "stopping": stopping,
                },
            )
            if (args.once or stopping) and not active:
                break
            time.sleep(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
