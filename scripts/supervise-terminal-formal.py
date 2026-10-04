#!/usr/bin/env python3
"""Keep paired Terminal-Bench-Evo arms busy for a bounded overnight run.

The supervisor never touches model-server processes.  It starts a fresh,
isolated repetition when an arm finishes, and writes an append-only health
snapshot every 30 minutes (plus one at startup and shutdown).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARM_SCRIPT = PROJECT_ROOT / "scripts" / "run-terminal-fix-git-arm.sh"
ARMS = {
    "static": ("terminus2_baseline", 8040, "rsi-formal-static"),
    "fixed-loop": ("terminus2_evomem", 8042, "rsi-formal-fixed"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def tmux_alive(session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def latest_rep(root: Path, arm: str) -> int:
    values: list[int] = []
    for entry in root.glob("rep-*/" + arm):
        try:
            values.append(int(entry.parent.name.removeprefix("rep-")))
        except ValueError:
            continue
    return max(values, default=0)


def launch(root: Path, arm: str) -> dict[str, object]:
    variant, port, session = ARMS[arm]
    rep = latest_rep(root, arm) + 1
    trials_dir = root / f"rep-{rep:03d}" / arm
    trials_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            str(ARM_SCRIPT),
            variant,
            str(port),
            str(trials_dir),
        ],
        check=True,
        cwd=PROJECT_ROOT,
    )
    return {"event": "launch", "at": now_iso(), "arm": arm, "rep": rep, "trials_dir": str(trials_dir)}


def endpoint_health(port: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/models",
        headers={"Authorization": "Bearer EMPTY"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {
                "ok": response.status == 200,
                "status": response.status,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def result_counts(root: Path, arm: str) -> dict[str, object]:
    results = list(root.glob(f"rep-*/{arm}/*/result.json"))
    rewards: list[float] = []
    exceptions = 0
    invalid = 0
    for path in results:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            verifier = data.get("verifier_result") or {}
            reward = (verifier.get("rewards") or {}).get("reward")
            if reward is not None:
                rewards.append(float(reward))
            else:
                invalid += 1
            if data.get("exception_info"):
                exceptions += 1
        except Exception:  # noqa: BLE001
            invalid += 1
    return {
        "finished": len(results),
        "passed": sum(value > 0 for value in rewards),
        "reward_sum": sum(rewards),
        "exceptions": exceptions,
        "invalid_results": invalid,
    }


def snapshot(root: Path, started: float, deadline: float, event: str = "health") -> dict[str, object]:
    disk = shutil.disk_usage(PROJECT_ROOT)
    arms: dict[str, object] = {}
    for arm, (_variant, port, session) in ARMS.items():
        arms[arm] = {
            "tmux_alive": tmux_alive(session),
            "latest_rep": latest_rep(root, arm),
            "endpoint": endpoint_health(port),
            "results": result_counts(root, arm),
        }
    return {
        "event": event,
        "at": now_iso(),
        "elapsed_sec": round(time.time() - started, 1),
        "remaining_sec": round(max(0.0, deadline - time.time()), 1),
        "disk_free_gib": round(disk.free / (1024 ** 3), 2),
        "arms": arms,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=10.0)
    parser.add_argument("--monitor-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "_supervisor"
    events_path = logs / "events.jsonl"
    latest_path = logs / "latest.json"
    started = time.time()
    deadline = started + args.hours * 3600
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    manifest = {
        "started_at": now_iso(),
        "duration_hours": args.hours,
        "deadline_epoch": deadline,
        "monitor_seconds": args.monitor_seconds,
        "model": "Qwen/Qwen3.8-27B",
        "benchmark": "Terminal-Bench-Evo",
        "chain": "fix-git",
        "arms": {
            arm: {"variant": values[0], "port": values[1], "tmux_session": values[2]}
            for arm, values in ARMS.items()
        },
    }
    atomic_json(logs / "manifest.json", manifest)
    first = snapshot(root, started, deadline, "startup")
    append_jsonl(events_path, first)
    atomic_json(latest_path, first)
    next_monitor = time.time() + args.monitor_seconds

    while not stopping and time.time() < deadline:
        for arm, (_variant, _port, session) in ARMS.items():
            if not tmux_alive(session):
                event = launch(root, arm)
                append_jsonl(events_path, event)
        if time.time() >= next_monitor:
            health = snapshot(root, started, deadline)
            append_jsonl(events_path, health)
            atomic_json(latest_path, health)
            while next_monitor <= time.time():
                next_monitor += args.monitor_seconds
        time.sleep(max(1, args.poll_seconds))

    final = snapshot(root, started, deadline, "shutdown")
    append_jsonl(events_path, final)
    atomic_json(latest_path, final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
