"""Guard the clean scheduler without terminating or duplicating solve workers."""

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
from rsi_harness.clean_rsi import atomic_json, read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "guard.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child, starts, stopping, next_snapshot = None, 0, False, 0

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        status = read_json(root / "status.json", {})
        if child and child.poll() is not None:
            print(
                json.dumps(
                    {
                        "event": "scheduler_exit",
                        "code": child.returncode,
                        "time": time.time(),
                    }
                ),
                flush=True,
            )
            child = None
        terminal = status.get("terminal")
        paused = (root / "PAUSE").exists()
        if child is None and not terminal and not paused and starts < 5:
            with (root / "supervisor.lock").open("a+") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass  # A manually launched supervisor owns the queue.
                else:
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    with (root / "supervisor.log").open("ab", buffering=0) as stream:
                        child = subprocess.Popen(
                            [
                                "bash",
                                str(PROJECT / "scripts/launch-clean-rsi.sh"),
                                "--root",
                                str(root),
                            ],
                            cwd=PROJECT,
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    starts += 1
                    print(
                        json.dumps(
                            {
                                "event": "scheduler_start",
                                "pid": child.pid,
                                "time": time.time(),
                                "starts": starts,
                            }
                        ),
                        flush=True,
                    )
        now = time.time()
        report = {
            "time": now,
            "guard_pid": os.getpid(),
            "scheduler_starts": starts,
            "scheduler_pid": child.pid if child else None,
            "paused": paused,
            "terminal": terminal,
            "heartbeat_age": now - status["time"] if status.get("time") else None,
            "needs_review": starts >= 5 and child is None and not terminal,
            "summary": status.get("summary"),
            "active": status.get("active"),
            "blocked": status.get("blocked"),
            "phase": status.get("phase"),
            "endpoints": status.get("endpoints"),
        }
        report["stale"] = (report["heartbeat_age"] or 0) > 300
        atomic_json(root / "monitor.json", report)
        if now >= next_snapshot:
            snapshot = (
                root / "monitor_snapshots" / (time.strftime("%Y%m%d-%H%M%S") + ".json")
            )
            atomic_json(snapshot, report)
            next_snapshot = now + 1800
        if terminal or report["needs_review"]:
            return 0 if terminal == "complete" else 2
        time.sleep(20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
