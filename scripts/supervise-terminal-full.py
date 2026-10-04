#!/usr/bin/env python3
"""Resumable, no-deadline full Terminal-Bench-Evo scheduler."""

from __future__ import annotations

import argparse
import faulthandler
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from rsi_harness.terminal_infra import docker_disk_status, live_chain_workers, readiness

DATASET = (
    PROJECT_ROOT
    / "benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo"
)
RUNNER = PROJECT_ROOT / "scripts/run-terminal-chain-arm.sh"
PORTS = (8040, 8041, 8042, 8043, 8044)
ARMS = {
    "baseline": "terminus2_baseline",
    "fixed-loop": "terminus2_evomem",
    "self-action": "terminus2_self_action",
    "open-ended": "terminus2_open_ended",
}
ALL_ARMS = ARMS.copy()
VERIFIER_INFRA_FAILURE_MARKERS = (
    "curl: command not found",
    "uvx: command not found",
    "/root/.local/bin/env: No such file or directory",
    "Unable to locate package curl",
)


class AdoptedWorker:
    """Monitor an existing runner without restarting its in-flight trial."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None  # A non-parent cannot recover an exit status.
        self.start_ticks = self._identity()
        if self.start_ticks is None:
            raise ValueError(f"worker {pid} is no longer running")

    def _identity(self):
        try:
            fields = (
                Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
            )
            return None if fields[0] == "Z" else fields[19]
        except (FileNotFoundError, ProcessLookupError):
            return None

    def poll(self):
        return None if self._identity() == self.start_ticks else 0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def endpoint_health(port: int) -> dict[str, object]:
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": "Bearer EMPTY"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return {
                "ok": response.status == 200,
                "status": response.status,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def discover_chains() -> list[str]:
    return sorted(
        child.name
        for child in DATASET.iterdir()
        if child.is_dir()
        and "-EVO-" not in child.name
        and (child / "task.toml").is_file()
        and (child / "instruction.md").is_file()
    )


def chain_tasks(chain: str) -> list[str]:
    tasks = [chain]
    tasks.extend(
        child.name
        for child in sorted(DATASET.glob(f"{chain}-EVO-*"))
        if child.is_dir() and (child / "task.toml").is_file()
    )
    tasks.extend(
        child.name
        for child in sorted(DATASET.glob(f"{chain}-v-*"))
        if child.is_dir() and (child / "task.toml").is_file()
    )
    return tasks


def valid_result(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("exception_info"):
            return False
        reward = ((value.get("verifier_result") or {}).get("rewards") or {}).get(
            "reward"
        )
        if reward is None:
            return False
        float(reward)

        verifier_stdout = (path.parent / "verifier" / "test-stdout.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        return not any(
            marker in verifier_stdout for marker in VERIFIER_INFRA_FAILURE_MARKERS
        )
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return False


def completed_task_names(output: Path) -> set[str]:
    completed: set[str] = set()
    if not output.is_dir():
        return completed
    for result in output.glob("*/result.json"):
        if valid_result(result):
            try:
                value = json.loads(result.read_text(encoding="utf-8"))
                task_name = value.get("task_name")
                if isinstance(task_name, str) and task_name:
                    completed.add(task_name)
            except (OSError, json.JSONDecodeError):
                pass
    return completed


class WorkProgress(TypedDict):
    expected: int
    valid: int
    missing: list[str]


def work_progress(root: Path, chain: str, arm: str) -> WorkProgress:
    expected = chain_tasks(chain)
    completed = completed_task_names(root / chain / arm)
    return {
        "expected": len(expected),
        "valid": sum(task in completed for task in expected),
        "missing": [task for task in expected if task not in completed],
    }


def work_complete(root: Path, chain: str, arm: str) -> bool:
    progress = work_progress(root, chain, arm)
    return progress["valid"] == progress["expected"]


def repaired_environment_ticket(root: Path, chain: str, arm: str) -> str | None:
    """One recovery ticket only after a failed environment gets fresh evidence.

    Agent/model timeouts are deliberately excluded: environment smoke does not
    fix them and must not become a way to repeatedly sample the same task.
    """
    missing = work_progress(root, chain, arm)["missing"]
    if not missing:
        return None
    task = missing[0]
    evidence = readiness(root, task)
    if evidence.get("status") != "ready":
        return None
    candidates = []
    for path in (root / chain / arm).glob("*/result.json"):
        try:
            result = json.loads(path.read_text())
            if result.get("task_name") == task:
                candidates.append((path.stat().st_mtime, result))
        except (OSError, ValueError):
            continue
    if not candidates:
        return None
    modified, result = max(candidates, key=lambda item: item[0])
    error = result.get("exception_info") or {}
    kind = error.get("exception_type", "")
    message = error.get("exception_message", "")
    environment_failure = kind.startswith("Environment") or (
        kind == "RuntimeError" and message.startswith("Docker compose command failed")
    )
    if environment_failure and evidence.get("finished", 0) > modified:
        return evidence["fingerprint"]
    return None


def next_image_ready(
    root: Path,
    chain: str,
    arm: str,
    require_preflight: bool = False,
    first_only: bool = False,
) -> bool:
    """Do not spend model-worker attempts on images still being prepared."""
    missing = work_progress(root, chain, arm)["missing"]
    if not missing:
        return True
    if first_only:
        missing = missing[:1]
    if require_preflight:
        # A full-chain worker can advance without consulting the scheduler;
        # a paired-stage worker is restricted to its first missing task.
        for task in missing:
            evidence = readiness(root, task)
            if evidence.get("status") != "ready":
                return False
            try:
                image = subprocess.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        evidence["image"],
                        "--format",
                        "{{.Id}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if image.returncode or image.stdout.strip() != evidence["image_id"]:
                    return False
            except (OSError, KeyError, subprocess.TimeoutExpired):
                return False
        return True
    task = DATASET / missing[0]
    match = re.search(
        r'^\s*docker_image\s*=\s*"([^"]+)"',
        (task / "task.toml").read_text(),
        re.MULTILINE,
    )
    if not match:
        # Locally built EVO stages use the prepared base images.
        return True
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", match[1], "--format", "{{.Id}}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def chain_images_ready(
    root: Path, chain: str, state: dict[str, Any] | None = None
) -> bool:
    """Check every stage still needed by a runnable arm before opening a chain."""
    tasks = chain_tasks(chain)
    if state is not None:
        needed = set()
        for arm in ARMS:
            if state["work"][f"{chain}/{arm}"].get("status") in {"pending", "running"}:
                needed.update(work_progress(root, chain, arm)["missing"])
        tasks = [task for task in tasks if task in needed]
    for task in tasks:
        evidence = readiness(root, task)
        if evidence.get("status") != "ready":
            return False
        try:
            image = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    evidence["image"],
                    "--format",
                    "{{.Id}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if image.returncode or image.stdout.strip() != evidence["image_id"]:
                return False
        except (OSError, KeyError, subprocess.TimeoutExpired):
            return False
    return True


def work_order(chains: list[str]) -> list[tuple[str, str]]:
    # Batch chains by available endpoints while preserving the selected arms.
    ordered: list[tuple[str, str]] = []
    for offset in range(0, len(chains), len(PORTS)):
        batch = chains[offset : offset + len(PORTS)]
        for arm in ARMS:
            ordered.extend((chain, arm) for chain in batch)
    return ordered


def paired_work_order(
    root: Path,
    state: dict[str, Any],
    chains: list[str],
    active: dict[str, tuple[int, str, str, Any]],
) -> list[tuple[str, str, int]]:
    """Prioritize completing selected-arm task cohorts, with no idle-slot barrier.

    Results from earlier runs remain in place. A pending arm is eligible only
    for its first missing stage, so chain memory always flows forward.
    """
    active_tasks = {
        state["work"][f"{chain}/{arm}"].get("task")
        for _port, chain, arm, process in active.values()
        if process.poll() is None
    }
    groups: list[tuple[tuple[int, int, int, int], list[tuple[str, str, int]]]] = []
    for chain_number, chain in enumerate(chains):
        tasks = chain_tasks(chain)
        completed = {arm: completed_task_names(root / chain / arm) for arm in ARMS}
        for stage_index, task in enumerate(tasks, start=1):
            members = []
            for arm in ARMS:
                key = f"{chain}/{arm}"
                if state["work"][key].get("status") != "pending":
                    continue
                if (
                    next((name for name in tasks if name not in completed[arm]), None)
                    == task
                ):
                    members.append((key, task, stage_index))
            if members:
                valid_arms = sum(task in completed[arm] for arm in ARMS)
                priority = (
                    0 if task in active_tasks else 1,
                    -valid_arms,
                    stage_index,
                    chain_number,
                )
                groups.append((priority, members))
    groups.sort(key=lambda group: group[0])
    return [member for _priority, members in groups for member in members]


def chain_priority_order(
    root: Path,
    state: dict[str, Any],
    chains: list[str],
    active: dict[str, tuple[int, str, str, Any]],
) -> list[tuple[str, str, int]]:
    """Finish runnable started chains before opening another independent chain.

    An incomplete arm blocks new work on its chain until a separately justified
    recovery requeues it. Existing in-flight workers are never interrupted.
    """
    active_chains = {
        chain
        for _port, chain, _arm, process in active.values()
        if process.poll() is None
    }
    groups: list[tuple[tuple[int, int, int], list[tuple[str, str, int]]]] = []
    for chain_number, chain in enumerate(chains):
        if any(
            state["work"][f"{chain}/{arm}"].get("status") == "incomplete"
            for arm in ARMS
        ):
            continue
        tasks = chain_tasks(chain)
        members: list[tuple[int, int, str, str, int]] = []
        total_valid = 0
        for arm_number, arm in enumerate(ARMS):
            completed = completed_task_names(root / chain / arm)
            valid = sum(task in completed for task in tasks)
            total_valid += valid
            key = f"{chain}/{arm}"
            if state["work"][key].get("status") != "pending":
                continue
            missing = next(
                (
                    (index, task)
                    for index, task in enumerate(tasks, 1)
                    if task not in completed
                ),
                None,
            )
            if missing is not None:
                stage_index, task = missing
                members.append((valid, arm_number, key, task, stage_index))
        if not members:
            continue
        priority = (
            0 if chain in active_chains else 1 if total_valid else 2,
            -total_valid,
            chain_number,
        )
        members.sort()  # Give lagging arms the next free worker.
        groups.append(
            (priority, [(key, task, stage) for _, _, key, task, stage in members])
        )
    groups.sort(key=lambda group: group[0])
    return [member for _priority, members in groups for member in members]


def select_chain_candidate(
    candidates: list[tuple[str, str, int]],
    active_chains: set[str],
    target_active_chains: int,
    eligible: Callable[[tuple[str, str, int]], bool],
) -> tuple[str, str, int] | None:
    """Prefer the active-chain window but never leave a free endpoint idle."""
    for item in candidates:
        chain = item[0].split("/", 1)[0]
        if (
            chain in active_chains or len(active_chains) < target_active_chains
        ) and eligible(item):
            return item
    return next((item for item in candidates if eligible(item)), None)


def worker_conflicts(active, chain: str, arm: str, allow_same_chain_arms: bool) -> bool:
    return any(
        item[1] == chain and (not allow_same_chain_arms or item[2] == arm)
        for item in active.values()
    )


def fresh_state(chains: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "model": "Qwen/Qwen3.8-27B",
        "benchmark": "Terminal-Bench-Evo",
        "no_deadline": True,
        "chains": chains,
        "arms": list(ARMS),
        "work": {
            f"{chain}/{arm}": {"status": "pending", "attempts": 0}
            for chain, arm in work_order(chains)
        },
    }


def load_state(path: Path, chains: list[str], retry_incomplete: bool) -> dict[str, Any]:
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = fresh_state(chains)
    expected = fresh_state(chains)["work"]
    work = state.setdefault("work", {})
    paused_work = state.setdefault("paused_work", {})
    for key in list(work):
        if key not in expected:
            paused_work[key] = work.pop(key)
    for key, default in expected.items():
        if key not in work:
            work[key] = paused_work.pop(key, default)
    state["arms"] = list(ARMS)
    for record in work.values():
        if retry_incomplete and record.get("status") == "incomplete":
            record["status"] = "pending"
            record["attempts"] = 0
        elif record.get("status") == "running":
            record["status"] = "pending"
    return state


def snapshot(
    root: Path,
    state: dict[str, Any],
    active: dict[str, tuple[int, str, str, subprocess.Popen[bytes] | AdoptedWorker]],
    event: str,
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for record in state["work"].values():
        status = str(record.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    valid = expected = paired_valid = 0
    for key in state["work"]:
        chain, arm = key.split("/", 1)
        progress = work_progress(root, chain, arm)
        valid += int(progress["valid"])
        expected += int(progress["expected"])
    for chain in state["chains"]:
        matched = set.intersection(
            *(completed_task_names(root / chain / arm) for arm in ARMS)
        )
        paired_valid += len(matched)
    return {
        "event": event,
        "supervisor_pid": os.getpid(),
        "at": now_iso(),
        "work_status": status_counts,
        "paused_arms": [arm for arm in ALL_ARMS if arm not in ARMS],
        "paused_work_count": len(state.get("paused_work", {})),
        "trials": {
            "valid": valid,
            "expected": expected,
            "paired_valid": paired_valid,
        },
        "scheduler": state.get("scheduler", {}),
        "disk": docker_disk_status(PROJECT_ROOT),
        "endpoints": {str(port): endpoint_health(port) for port in PORTS},
        "active": {
            slot: {
                "port": item[0],
                "chain": item[1],
                "arm": item[2],
                "pid": item[3].pid,
                "endpoint": endpoint_health(port),
            }
            for slot, item in active.items()
            if item[3].poll() is None
            for port in (item[0],)
        },
    }


def launch(
    root: Path, port: int, chain: str, arm: str, stage_index: int | None = None
) -> subprocess.Popen[bytes]:
    output = root / chain / arm
    output.mkdir(parents=True, exist_ok=True)
    log_path = root / "_supervisor" / "workers" / f"{port}-{chain}-{arm}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab", buffering=0)
    command = [str(RUNNER), ARMS[arm], str(port), chain, str(output)]
    if stage_index is not None:
        command.append(str(stage_index))
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    return process


def main() -> int:
    global ARMS
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=tuple(ALL_ARMS),
        default=list(ALL_ARMS),
        help="Active experiment arms; omitted arms are archived, not scheduled.",
    )
    parser.add_argument("--monitor-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--max-work-attempts", type=int, default=3)
    parser.add_argument("--auto-adopt", action="store_true")
    parser.add_argument("--require-preflight", action="store_true")
    parser.add_argument(
        "--paired-stages",
        action="store_true",
        help="Schedule one task stage at a time, prioritizing its four arms.",
    )
    parser.add_argument(
        "--chain-priority",
        action="store_true",
        help="Finish active and partial chains before opening new chains.",
    )
    parser.add_argument(
        "--target-active-chains",
        type=int,
        default=2,
        help="Soft limit for concurrently active chains; never idle a healthy endpoint.",
    )
    parser.add_argument(
        "--allow-same-chain-arms",
        action="store_true",
        help="Allow isolated arms to share a preflighted immutable image cache.",
    )
    parser.add_argument(
        "--detach-on-stop",
        action="store_true",
        help="Leave live chain workers running on scheduler stop for safe handover.",
    )
    parser.add_argument(
        "--adopt-worker",
        nargs=4,
        action="append",
        default=[],
        metavar=("PORT", "CHAIN", "ARM", "PID"),
        help="Take over a verified live chain runner without interrupting it.",
    )
    parser.add_argument(
        "--workers-per-endpoint",
        type=int,
        default=1,
        help=(
            "Number of task workers sharing each model endpoint. The model server "
            "still enforces its own inference concurrency; extra workers overlap "
            "environment, tool, and verifier time."
        ),
    )
    parser.add_argument(
        "--retry-incomplete",
        action="store_true",
        help="Requeue work previously marked incomplete; valid tasks are still skipped.",
    )
    args = parser.parse_args()
    if len(args.arms) != len(set(args.arms)):
        parser.error("--arms must not contain duplicates")
    ARMS = {arm: ALL_ARMS[arm] for arm in args.arms}
    if args.allow_same_chain_arms and not args.require_preflight:
        parser.error("--allow-same-chain-arms requires --require-preflight")
    if args.chain_priority and not (args.paired_stages and args.require_preflight):
        parser.error(
            "--chain-priority requires --paired-stages and --require-preflight"
        )
    if args.target_active_chains < 1:
        parser.error("--target-active-chains must be positive")

    root = args.root.resolve()
    logs = root / "_supervisor"
    logs.mkdir(parents=True, exist_ok=True)
    # Keep this handle open for the scheduler lifetime. A second supervisor
    # must not race state.json or launch the same unfinished chain twice.
    instance_lock = (logs / "supervisor.lock").open("a+")
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error(f"another supervisor already owns {root}")
    state_path = logs / "state.json"
    events = logs / "events.jsonl"
    latest = logs / "latest.json"
    chains = discover_chains()
    state = load_state(state_path, chains, args.retry_incomplete)
    state["scheduler"] = {
        "mode": "chain_priority"
        if args.chain_priority
        else "paired_stages"
        if args.paired_stages
        else "legacy",
        "target_active_chains": args.target_active_chains
        if args.chain_priority
        else None,
        "soft_limit": args.chain_priority,
        "arms": list(ARMS),
    }
    workers_per_endpoint = max(1, args.workers_per_endpoint)
    slots = [
        (f"{port}-{replica + 1}", port)
        for port in PORTS
        for replica in range(workers_per_endpoint)
    ]
    active: dict[
        str, tuple[int, str, str, subprocess.Popen[bytes] | AdoptedWorker]
    ] = {}
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    for key, record in state["work"].items():
        chain, arm = key.split("/", 1)
        if work_complete(root, chain, arm):
            record["status"] = "complete"
            record["completed_at"] = record.get("completed_at", now_iso())
        elif (
            record.get("status") in {"complete", "running"}
            or args.retry_incomplete
            and record.get("status") == "incomplete"
        ):
            record["status"] = "pending"
    atomic_json(state_path, state)

    adopted = list(args.adopt_worker)
    if args.auto_adopt:
        explicit_pids = {entry[3] for entry in adopted}
        adopted.extend(w for w in live_chain_workers(root) if w[3] not in explicit_pids)
    for port_text, chain, arm, pid_text in adopted:
        port, pid = int(port_text), int(pid_text)
        key = f"{chain}/{arm}"
        if port not in PORTS or key not in state["work"]:
            parser.error(f"invalid adopted worker: {port} {key}")
        try:
            proc = Path(f"/proc/{pid}")
            command = proc.joinpath("cmdline").read_bytes().decode().split("\0")
            environment = proc.joinpath("environ").read_bytes().decode().split("\0")
        except FileNotFoundError:
            continue  # It completed during handover; resume checks skip its results.
        runner = str(DATASET.parent / "harbor_EvoMem/scripts/run_chain.py")
        expected_output = str(root / chain / arm)
        env_file = str(
            DATASET.parent / f"harbor_EvoMem/scripts/terminus2_llm_{port}.env"
        )
        if (
            runner not in command
            or expected_output not in command
            or f"HARBOR_EVOMEM_LLM_ENV={env_file}" not in environment
            or proc.stat().st_uid != os.getuid()
        ):
            parser.error(f"PID {pid} does not match requested worker {key} on {port}")
        slot = next((s for s, p in slots if p == port and s not in active), None)
        if slot is None or worker_conflicts(
            active, chain, arm, args.allow_same_chain_arms
        ):
            parser.error(f"duplicate adopted worker for {key} or port {port}")
        try:
            process = AdoptedWorker(pid)
        except ValueError:
            continue
        active[slot] = (port, chain, arm, process)
        progress = work_progress(root, chain, arm)
        state["work"][key].update(
            status="running",
            pid=pid,
            port=port,
            slot=slot,
            adopted_at=now_iso(),
            attempts=max(1, int(state["work"][key].get("attempts", 0))),
            progress_before=progress,
            task=(
                chain_tasks(chain)[int(command[command.index("--start-index") + 1]) - 1]
                if "--start-index" in command
                else None
            ),
        )
        append_jsonl(
            events,
            {
                "event": "adopt",
                "at": now_iso(),
                "pid": pid,
                "chain": chain,
                "arm": arm,
                "port": port,
            },
        )
    atomic_json(state_path, state)

    startup = snapshot(root, state, active, "startup")
    atomic_json(latest, startup)
    append_jsonl(events, startup)
    next_monitor = time.time() + args.monitor_seconds
    order = [f"{chain}/{arm}" for chain, arm in work_order(chains)]

    try:
        while not stopping:
            atomic_json(
                logs / "heartbeat.json",
                {
                    "pid": os.getpid(),
                    "at": now_iso(),
                    "time": time.time(),
                    "active": len(active),
                },
            )
            for slot, (port, chain, arm, process) in list(active.items()):
                if process.poll() is None:
                    continue
                key = f"{chain}/{arm}"
                record = state["work"][key]
                complete = work_complete(root, chain, arm)
                progress = work_progress(root, chain, arm)
                attempts = int(record.get("attempts", 0))
                advanced = progress["valid"] > int(
                    (record.get("progress_before") or {}).get("valid", 0)
                )
                next_status = (
                    "complete"
                    if complete
                    else "pending"
                    if (args.paired_stages and advanced)
                    or attempts < max(1, args.max_work_attempts)
                    else "incomplete"
                )
                record.update(
                    {
                        "status": next_status,
                        "finished_at": now_iso(),
                        "exit_code": process.returncode,
                        "progress": progress,
                    }
                )
                if args.paired_stages and advanced:
                    record["attempts"] = 0
                append_jsonl(
                    events,
                    {
                        "event": "worker_exit",
                        "at": now_iso(),
                        "slot": slot,
                        "port": port,
                        "chain": chain,
                        "arm": arm,
                        "exit_code": process.returncode,
                        "complete": complete,
                        "progress": record["progress"],
                    },
                )
                del active[slot]
                atomic_json(state_path, state)

            active_chains = {
                item[1] for item in active.values() if item[3].poll() is None
            }
            ready_cache: dict[str, bool] = {}

            if args.require_preflight:
                for key, record in state["work"].items():
                    if record.get("status") != "incomplete":
                        continue
                    ticket = repaired_environment_ticket(root, *key.split("/", 1))
                    if ticket and ticket != record.get("repair_ticket"):
                        record.update(
                            status="pending", attempts=0, repair_ticket=ticket
                        )
                        append_jsonl(
                            events,
                            {
                                "event": "environment_repaired",
                                "at": now_iso(),
                                "work": key,
                                "ticket": ticket,
                            },
                        )
                        atomic_json(state_path, state)

            def ready(key: str, cache=ready_cache) -> bool:
                if key not in cache:
                    cache[key] = next_image_ready(
                        root,
                        *key.split("/", 1),
                        require_preflight=args.require_preflight,
                        first_only=args.paired_stages,
                    )
                return cache[key]

            chain_ready_cache: dict[str, bool] = {}

            def chain_ready(chain: str, cache=chain_ready_cache) -> bool:
                if chain not in cache:
                    cache[chain] = chain_images_ready(root, chain, state)
                return cache[chain]

            disk_ok = docker_disk_status(PROJECT_ROOT)["ok"]
            candidates = (
                chain_priority_order(root, state, chains, active)
                if args.chain_priority
                else paired_work_order(root, state, chains, active)
                if args.paired_stages
                else [(key, None, None) for key in order]
            )
            for slot, port in slots:
                if not disk_ok:
                    break  # Leave existing workers alone; do not fill a nearly full disk.
                if slot in active or not endpoint_health(port).get("ok"):
                    continue

                def eligible(
                    item: tuple[str, str, int], active_now=active_chains
                ) -> bool:
                    key = item[0]
                    chain_name = key.split("/", 1)[0]
                    return (
                        state["work"][key].get("status") == "pending"
                        and (args.allow_same_chain_arms or chain_name not in active_now)
                        and (not args.chain_priority or chain_ready(chain_name))
                        and ready(key)
                    )

                selected = (
                    select_chain_candidate(
                        candidates, active_chains, args.target_active_chains, eligible
                    )
                    if args.chain_priority
                    else next((item for item in candidates if eligible(item)), None)
                )
                if selected is None:
                    continue
                key, task, stage_index = selected
                chain, arm = key.split("/", 1)
                progress_before = work_progress(root, chain, arm)
                process = launch(root, port, chain, arm, stage_index)
                active[slot] = (port, chain, arm, process)
                candidates.remove(selected)
                active_chains.add(chain)
                record = state["work"][key]
                record.update(
                    {
                        "status": "running",
                        "attempts": int(record.get("attempts", 0)) + 1,
                        "started_at": now_iso(),
                        "slot": slot,
                        "port": port,
                        "pid": process.pid,
                        "task": task,
                        "progress_before": progress_before,
                    }
                )
                append_jsonl(
                    events,
                    {
                        "event": "launch",
                        "at": now_iso(),
                        "slot": slot,
                        "port": port,
                        "chain": chain,
                        "arm": arm,
                        "task": task,
                        "pid": process.pid,
                    },
                )
                atomic_json(state_path, state)

            pending = any(
                record.get("status") == "pending" for record in state["work"].values()
            )
            if not pending and not active:
                break
            if time.time() >= next_monitor:
                report = snapshot(root, state, active, "health")
                atomic_json(latest, report)
                append_jsonl(events, report)
                while next_monitor <= time.time():
                    next_monitor += args.monitor_seconds
            time.sleep(max(1, args.poll_seconds))
    finally:
        if stopping and not args.detach_on_stop:
            for _port, _chain, _arm, process in active.values():
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        final = snapshot(root, state, active, "interrupted" if stopping else "finished")
        atomic_json(latest, final)
        append_jsonl(events, final)
        atomic_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
