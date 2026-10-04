#!/usr/bin/env python3
"""Phase-2 scheduler: finish controls, then rotate three native RSI arms."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run-terminal-fix-git-arm.sh"
SLOTS = {
    8040: {
        "blocker": "rsi-formal-static",
        "arm": "open-ended",
        "variant": "terminus2_open_ended",
        "session": "rsi-formal-open-ended",
    },
    8042: {
        "blocker": "rsi-formal-fixed",
        "arm": "evolution",
        "variant": "terminus2_evolution",
        "session": "rsi-formal-evolution",
    },
    8043: {
        "blocker": None,
        "arm": "self-action",
        "variant": "terminus2_self_action",
        "session": "rsi-formal-self-action",
    },
}
ALL_SESSIONS = {
    "static": "rsi-formal-static",
    "fixed-loop": "rsi-formal-fixed",
    "self-action": "rsi-formal-self-action",
    "open-ended": "rsi-formal-open-ended",
    "evolution": "rsi-formal-evolution",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def alive(session: str | None) -> bool:
    if not session:
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def append(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def next_rep(root: Path, arm: str) -> int:
    reps: list[int] = []
    for path in root.glob(f"rep-*/{arm}"):
        try:
            reps.append(int(path.parent.name.removeprefix("rep-")))
        except ValueError:
            pass
    return max(reps, default=0) + 1


def previous_rep_is_complete(root: Path, arm: str) -> tuple[bool, str]:
    next_value = next_rep(root, arm)
    if next_value == 1:
        return True, "first_rep"
    summary = root / f"rep-{next_value - 1:03d}" / arm / "_logs" / "chain_runner_summary.json"
    if not summary.is_file():
        return False, f"missing_summary:{summary}"
    try:
        value = json.loads(summary.read_text(encoding="utf-8"))
        tasks = (value.get("chains") or {}).get("fix-git") or []
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid_summary:{type(exc).__name__}:{exc}"
    if len(tasks) != 5:
        return False, f"incomplete_task_count:{len(tasks)}"
    failures = [entry for entry in tasks if entry.get("exit_code") != 0]
    if failures:
        return False, f"nonzero_task_exits:{len(failures)}"
    return True, "complete"


def launch(root: Path, port: int, spec: dict[str, str | None]) -> dict[str, object]:
    arm = str(spec["arm"])
    rep = next_rep(root, arm)
    output = root / f"rep-{rep:03d}" / arm
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            str(spec["session"]),
            str(RUNNER),
            str(spec["variant"]),
            str(port),
            str(output),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return {"event": "launch", "at": now_iso(), "port": port, "arm": arm, "rep": rep, "output": str(output)}


def endpoint(port: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/models",
        headers={"Authorization": "Bearer EMPTY"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def counts(root: Path, arm: str) -> dict[str, object]:
    results = list(root.glob(f"rep-*/{arm}/*/result.json"))
    rewards: list[float] = []
    exceptions = 0
    invalid = 0
    for path in results:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            reward = (((value.get("verifier_result") or {}).get("rewards") or {}).get("reward"))
            if reward is None:
                invalid += 1
            else:
                rewards.append(float(reward))
            exceptions += int(bool(value.get("exception_info")))
        except Exception:  # noqa: BLE001
            invalid += 1
    return {
        "finished": len(results),
        "passed": sum(reward > 0 for reward in rewards),
        "reward_sum": sum(rewards),
        "exceptions": exceptions,
        "invalid": invalid,
    }


def health(root: Path, deadline: float, event: str = "health") -> dict[str, object]:
    usage = shutil.disk_usage(PROJECT_ROOT)
    return {
        "event": event,
        "at": now_iso(),
        "remaining_sec": round(max(0.0, deadline - time.time()), 1),
        "disk_free_gib": round(usage.free / 1024**3, 2),
        "endpoints": {str(port): endpoint(port) for port in SLOTS},
        "arms": {
            arm: {"tmux_alive": alive(session), "results": counts(root, arm)}
            for arm, session in ALL_SESSIONS.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--deadline-epoch", type=float, required=True)
    parser.add_argument("--monitor-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    root = args.root.resolve()
    log_dir = root / "_supervisor"
    events = log_dir / "phase2-events.jsonl"
    latest = log_dir / "phase2-latest.json"
    first = health(root, args.deadline_epoch, "phase2_startup")
    append(events, first)
    atomic(latest, first)
    next_health = time.time() + args.monitor_seconds
    blocked_reasons: dict[str, str] = {}

    while time.time() < args.deadline_epoch:
        for port, spec in SLOTS.items():
            if alive(spec["blocker"]):
                continue
            if not alive(str(spec["session"])):
                arm = str(spec["arm"])
                ready, reason = previous_rep_is_complete(root, arm)
                if ready:
                    blocked_reasons.pop(arm, None)
                    append(events, launch(root, port, spec))
                elif blocked_reasons.get(arm) != reason:
                    blocked_reasons[arm] = reason
                    append(
                        events,
                        {"event": "arm_blocked", "at": now_iso(), "port": port, "arm": arm, "reason": reason},
                    )
        if time.time() >= next_health:
            report = health(root, args.deadline_epoch)
            append(events, report)
            atomic(latest, report)
            while next_health <= time.time():
                next_health += args.monitor_seconds
        time.sleep(max(1, args.poll_seconds))

    final = health(root, args.deadline_epoch, "phase2_shutdown")
    append(events, final)
    atomic(latest, final)
    summary_output = root / "_analysis" / "summary.json"
    subprocess.run(
        [
            str(PROJECT_ROOT / "scripts" / "summarize-terminal-formal.py"),
            str(root),
            "--output",
            str(summary_output),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
