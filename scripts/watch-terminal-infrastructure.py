#!/usr/bin/env python3
"""Independent scheduler guard. Never kill or restart chain/model workers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.terminal_infra import atomic_json


def heartbeat_age(path: Path, pid: int, started: float) -> float:
    try:
        heartbeat = json.loads(path.read_text())
        if heartbeat["pid"] == pid:
            return max(0, time.time() - heartbeat["time"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return time.time() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    root = args.root.resolve()
    logs = root / "_supervisor"
    logs.mkdir(parents=True, exist_ok=True)
    lock = (logs / "watchdog.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stopping = False
    child = None
    started = 0.0
    restarts = 0
    next_start = 0.0
    terminal_state = None

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def event(kind, **fields):
        value = {"event": kind, "time": time.time(), **fields}
        with (logs / "watchdog-events.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")
        print(json.dumps(value), flush=True)

    try:
        while not stopping:
            # A persistent pause survives guard/scheduler restarts.
            paused = (logs / "PAUSE").exists()
            if child is not None and child.poll() is not None:
                event("scheduler_exit", pid=child.pid, exit_code=child.returncode)
                if child.returncode == 0:
                    try:
                        final = json.loads((logs / "latest.json").read_text())
                        if (
                            final.get("supervisor_pid") == child.pid
                            and final.get("event") == "finished"
                        ):
                            trials = final["trials"]
                            terminal_state = (
                                "complete"
                                if trials["valid"] == trials["expected"]
                                else "needs_review"
                            )
                            event("queue_terminal", state=terminal_state, trials=trials)
                    except (OSError, ValueError, KeyError):
                        pass
                child = None
                next_start = time.time() + min(60, 5 * max(1, restarts))
            if (
                child is None
                and not paused
                and not terminal_state
                and time.time() >= next_start
            ):
                # Avoid fighting a manually running scheduler. The child's own
                # lifetime lock provides the definitive race-free exclusion.
                with (logs / "supervisor.lock").open("a+") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        next_start = time.time() + 30
                    else:
                        fcntl.flock(probe, fcntl.LOCK_UN)
                        command = [
                            sys.executable,
                            "-u",
                            str(PROJECT / "scripts/supervise-terminal-full.py"),
                            "--root",
                            str(root),
                            "--arms",
                            "baseline",
                            "fixed-loop",
                            "self-action",
                            "--auto-adopt",
                            "--detach-on-stop",
                            "--require-preflight",
                            "--paired-stages",
                            "--chain-priority",
                            "--target-active-chains",
                            "2",
                            "--allow-same-chain-arms",
                            "--max-work-attempts",
                            "1",
                            "--monitor-seconds",
                            "60",
                            "--poll-seconds",
                            "20",
                        ]
                        with (logs / "supervisor-guarded.log").open(
                            "ab", buffering=0
                        ) as stream:
                            child = subprocess.Popen(
                                command,
                                cwd=PROJECT,
                                stdout=stream,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                            )
                        started = time.time()
                        restarts += 1
                        event("scheduler_start", pid=child.pid, starts=restarts)
            age = (
                heartbeat_age(logs / "heartbeat.json", child.pid, started)
                if child
                else None
            )
            if child and (paused or age > args.stale_seconds):
                event(
                    "scheduler_detach",
                    pid=child.pid,
                    reason="pause" if paused else "stale",
                    age=age,
                )
                if not paused:
                    child.send_signal(signal.SIGUSR1)  # Thread dump before recovery.
                child.terminate()  # detach-on-stop leaves independently-sessioned workers alone.
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()  # ONLY our Popen child, never its process group.
                    child.wait(timeout=10)
            atomic_json(
                logs / "watchdog.json",
                {
                    "pid": os.getpid(),
                    "time": time.time(),
                    "scheduler_pid": child.pid
                    if child and child.poll() is None
                    else None,
                    "heartbeat_age": age,
                    "starts": restarts,
                    "paused": paused,
                    "terminal_state": terminal_state,
                },
            )
            time.sleep(max(1, args.poll_seconds))
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        event("watchdog_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
