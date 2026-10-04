#!/usr/bin/env python3
"""Run four RSI conditions over five diverse Terminal-Bench-Evo chains.

Each model endpoint owns one chain and runs all conditions sequentially.  This
keeps chain state isolated, avoids duplicate Docker image builds, and uses all
five TP=2 Qwen endpoints at once.  Health snapshots are persisted every 30
minutes so a detached run can be audited without relying on terminal output.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run-terminal-chain-arm.sh"
SLOTS = {
    8040: "llm-inference-batching-scheduler",
    8041: "adaptive-rejection-sampler",
    8042: "cancel-async-tasks",
    8043: "model-extraction-relu-logits",
    8044: "regex-chess",
}
ARMS = {
    "baseline": "terminus2_baseline",
    "fixed-loop": "terminus2_evomem",
    "self-action": "terminus2_self_action",
    "open-ended": "terminus2_open_ended",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


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


def task_results(output: Path) -> dict[str, object]:
    rewards: list[float] = []
    exceptions = 0
    invalid = 0
    result_paths = list(output.glob("*/result.json"))
    for path in result_paths:
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
        "finished": len(result_paths),
        "passed": sum(reward > 0 for reward in rewards),
        "reward_sum": sum(rewards),
        "exceptions": exceptions,
        "invalid": invalid,
    }


def arm_complete(output: Path) -> bool:
    summary = output / "_logs" / "chain_runner_summary.json"
    if not summary.is_file():
        return False
    try:
        value = json.loads(summary.read_text(encoding="utf-8"))
        rows = next(iter((value.get("chains") or {}).values()))
        if not rows or not all(row.get("exit_code") == 0 for row in rows):
            return False
        result_paths = list(output.glob("*/result.json"))
        if len(result_paths) != len(rows):
            return False
        for path in result_paths:
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("exception_info"):
                return False
            reward = (((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"))
            if reward is None:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def snapshot(root: Path, processes: dict[int, tuple[str, subprocess.Popen[bytes]]], event: str) -> dict[str, object]:
    slots: dict[str, object] = {}
    for port, chain in SLOTS.items():
        active = processes.get(port)
        slots[str(port)] = {
            "chain": chain,
            "endpoint": endpoint_health(port),
            "active_arm": active[0] if active and active[1].poll() is None else None,
            "arms": {
                arm: {
                    "complete": arm_complete(root / chain / arm),
                    "results": task_results(root / chain / arm),
                }
                for arm in ARMS
            },
        }
    return {"event": event, "at": now_iso(), "slots": slots}


def launch(root: Path, port: int, chain: str, arm: str) -> subprocess.Popen[bytes]:
    output = root / chain / arm
    output.mkdir(parents=True, exist_ok=True)
    log_path = root / "_supervisor" / "workers" / f"{port}-{chain}-{arm}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        [str(RUNNER), ARMS[arm], str(port), chain, str(output)],
        cwd=PROJECT_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    return process


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=10.0)
    parser.add_argument("--monitor-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=20)
    args = parser.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "_supervisor"
    events = logs / "events.jsonl"
    latest = logs / "latest.json"
    deadline = time.time() + args.hours * 3600
    stopping = False
    processes: dict[int, tuple[str, subprocess.Popen[bytes]]] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    manifest = {
        "started_at": now_iso(),
        "model": "Qwen/Qwen3.8-27B",
        "benchmark": "Terminal-Bench-Evo",
        "hours": args.hours,
        "deadline_epoch": deadline,
        "ports_to_chains": SLOTS,
        "arms": ARMS,
        "design": "one chain per endpoint; four independent conditions sequentially per chain",
    }
    atomic_json(logs / "manifest.json", manifest)

    # Services may still be loading when the detached supervisor starts.
    while not stopping and time.time() < deadline:
        health = {port: endpoint_health(port) for port in SLOTS}
        if all(item.get("ok") for item in health.values()):
            break
        report = {"event": "waiting_for_endpoints", "at": now_iso(), "endpoints": health}
        atomic_json(latest, report)
        time.sleep(20)

    first = snapshot(root, processes, "startup")
    append_jsonl(events, first)
    atomic_json(latest, first)
    next_monitor = time.time() + args.monitor_seconds

    while not stopping:
        for port, chain in SLOTS.items():
            active = processes.get(port)
            if active and active[1].poll() is None:
                continue
            if active:
                arm, proc = active
                append_jsonl(
                    events,
                    {"event": "worker_exit", "at": now_iso(), "port": port, "chain": chain, "arm": arm, "exit_code": proc.returncode},
                )
                del processes[port]

            if time.time() >= deadline:
                continue
            next_arm = next((arm for arm in ARMS if not arm_complete(root / chain / arm)), None)
            if next_arm is not None and endpoint_health(port).get("ok"):
                proc = launch(root, port, chain, next_arm)
                processes[port] = (next_arm, proc)
                append_jsonl(
                    events,
                    {"event": "launch", "at": now_iso(), "port": port, "chain": chain, "arm": next_arm, "pid": proc.pid},
                )

        done = all(arm_complete(root / chain / arm) for chain in SLOTS.values() for arm in ARMS)
        if done or (time.time() >= deadline and not processes):
            break
        if time.time() >= next_monitor:
            report = snapshot(root, processes, "health")
            append_jsonl(events, report)
            atomic_json(latest, report)
            while next_monitor <= time.time():
                next_monitor += args.monitor_seconds
        time.sleep(max(1, args.poll_seconds))

    final = snapshot(root, processes, "shutdown" if not stopping else "interrupted")
    append_jsonl(events, final)
    atomic_json(latest, final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
