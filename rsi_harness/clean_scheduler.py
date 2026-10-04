"""Scheduler-only extension for the frozen clean RSI protocol.

Workers still run clean_campaign.stage unchanged. The original manifest remains
immutable; scheduler policy/code/order have a separate versioned manifest.
"""

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
from unittest.mock import patch

from . import clean_campaign as core
from .clean_rsi import ARMS, IMPROVE, SCHEMA, SOLVE, atomic_json, digest, read_json

POLICY = {
    "version": "paired-window-v2-baseline-repair",
    "window_chains": 3,
    "priority": "oldest-chain-then-earliest-missing-stage",
    "baseline_adoption": "metadata-only-no-inference-slot",
    "resource_lease": "whole-stage-including-tools-grader-improvement",
    "blocked_chain": "quarantine-no-new-dispatch-preserve-live-workers",
    "baseline_repair": "incompatible-history-pending-paired-gaps-first",
}
PREVIOUS_SCHEDULER_SHA256 = (
    "62956f6bd25dd8e181ac6ad74317cf7ed0bcf0891b45cd953e5dd74e294cd178"
)


def protocol_manifest(args):
    """Exactly the existing protocol, including its original four source hashes."""
    return {
        "schema": SCHEMA,
        "model": core.MODEL,
        "arms": ARMS,
        "solve": SOLVE,
        "improve": IMPROVE,
        "pilot": args.pilot,
        "old_root": str(args.old_root),
        "code": core.source_hashes(),
        "baseline_policy": "reuse-compatible-exclude-incompatible-no-repeat",
        "chain_reset": True,
        "selection": "protocol-only-no-reward-replay",
        "ports": core.PORTS,
    }


def initialize_scheduler(args):
    manifest = read_json(args.root / "manifest.json")
    expected = protocol_manifest(args)
    if manifest is None:
        if read_json(args.root / "active.json", {}) or any(
            args.root.glob("*/*/stages")
        ):
            raise RuntimeError(
                "cannot reconstruct missing protocol manifest for existing work"
            )
        atomic_json(args.root / "manifest.json", expected)
        manifest = expected
    if digest(manifest) != digest(expected):
        raise RuntimeError("frozen experiment protocol changed or manifest missing")
    path = args.root / "scheduler_manifest.json"
    all_chains = core.chains()
    if len(set(args.pilot)) != len(args.pilot) or any(
        c not in all_chains for c in args.pilot
    ):
        raise ValueError("invalid pilot chains")
    order = args.pilot + [c for c in all_chains if c not in args.pilot]
    candidate = {
        "policy": POLICY,
        "protocol_sha256": digest(manifest),
        "code_sha256": digest(Path(__file__).read_text()),
        "chain_order": order,
        "task_order": {c: core.tasks(c) for c in order},
    }
    existing = read_json(path)
    if existing and digest(existing) != digest(candidate):
        if not getattr(args, "migrate_baseline_repairs", False):
            raise RuntimeError(
                "scheduler policy/code/task order changed; audit before migration"
            )
        if (
            existing["policy"]["version"] != "paired-window-v1"
            or existing["code_sha256"] != PREVIOUS_SCHEDULER_SHA256
            or any(
                existing[k] != candidate[k]
                for k in ("protocol_sha256", "chain_order", "task_order")
            )
        ):
            raise RuntimeError(
                "baseline repair migration requires the audited v1 scheduler"
            )
        atomic_json(
            args.root / "scheduler_migrations" / "baseline-repair-v2.json",
            {
                "time": time.time(),
                "reason": "user authorized filling incompatible baseline gaps",
                "previous_manifest": existing,
                "new_manifest": candidate,
                "preserved_workers": read_json(args.root / "active.json", {}),
                "completed_record_hashes": {
                    str(p.relative_to(args.root)): digest(p.read_text())
                    for p in args.root.glob("*/*/stages/*/record.json")
                    if read_json(p).get("status") == "complete"
                },
                "excluded_records": {
                    str(p.relative_to(args.root)): read_json(p)
                    for p in args.root.glob("*/baseline/stages/*/record.json")
                    if read_json(p).get("status") == "excluded"
                },
            },
        )
        atomic_json(path, candidate)
    if not existing:
        atomic_json(path, candidate)
        atomic_json(
            args.root / "scheduler_transition.json",
            {
                "time": time.time(),
                "reason": "user requested step-one scheduling only",
                "from": "original-clean-campaign",
                "to": POLICY,
                "protocol_sha256": digest(manifest),
                "preserved_workers": read_json(args.root / "active.json", {}),
                "previous_status": read_json(args.root / "status.json", {}),
            },
        )
    return order


def adopt_baselines(root, old_root, order, active, lookup=core.legacy_baseline):
    """Import consecutive audited historical stages without borrowing a GPU slot.

    The same per-chain lock prevents racing live baseline workers. A launched
    trial is never overwritten, even if its worker died before writing a record.
    """
    adopted, waiting = [], []
    busy = {(w["chain"], w["arm"]) for w in active.values()}
    for chain in order:
        if (chain, "baseline") in busy:
            continue
        work = root / chain / "baseline"
        # No directory/lock is needed when this chain has no historical baseline.
        if not work.exists() and not (old_root / chain / "baseline").exists():
            continue
        work.mkdir(parents=True, exist_ok=True)
        with (work / "worker.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            for task in core.tasks(chain):
                folder = core.stage_path(root, chain, "baseline", task)
                path = folder / "record.json"
                previous = read_json(path)
                if previous:
                    if previous["status"] == "complete":
                        continue
                    break
                if (folder / "excluded_record.json").exists():
                    break
                if (folder / "launch.json").exists() or (folder / "trials").exists():
                    break
                reused = lookup(old_root, chain, task)
                if reused is None:
                    break  # A genuinely new predecessor must be solved first.
                if reused["status"] == "excluded":
                    atomic_json(
                        folder / "excluded_record.json", {**reused, "task": task}
                    )
                    break
                ok, why = core.ready(task, old_root)
                if not ok:
                    waiting.append({"chain": chain, "task": task, "reason": why})
                    break
                atomic_json(
                    path,
                    {
                        **reused,
                        "task": task,
                        "readiness": core.readiness(old_root, task),
                    },
                )
                adopted.append(
                    {"chain": chain, "task": task, "status": reused["status"]}
                )
    return adopted, waiting


def campaign_status(root, order):
    """Keep completed baseline successors visible while repairing earlier holes."""
    rows, pending, blocked, summary = core.status(root, order)
    rows = [r for r in rows if r["arm"] != "baseline"]
    pending = [p for p in pending if p[1] != "baseline"]
    blocked = [r for r in blocked if r["arm"] != "baseline"]
    for chain in order:
        first_missing = None
        chain_blocked = False
        for task in core.tasks(chain):
            folder = core.stage_path(root, chain, "baseline", task)
            record = read_json(folder / "repair_error.json") or read_json(
                folder / "record.json"
            )
            if record is None and (folder / "excluded_record.json").exists():
                record = {"status": "pending_repair"}
            if record:
                item = {**record, "chain": chain, "arm": "baseline", "task": task}
                rows.append(item)
                if record["status"] == "blocked":
                    blocked.append(item)
                    chain_blocked = True
            if (not record or record["status"] != "complete") and first_missing is None:
                first_missing = task
        if first_missing and not chain_blocked:
            pending.append((chain, "baseline", first_missing))
    complete = [r for r in rows if r["arm"] == "baseline" and r["status"] == "complete"]
    summary["baseline"].update(
        {
            "completed": len(complete),
            "passed": sum(r.get("reward", 0) > 0 for r in complete),
            "excluded": sum(
                r["arm"] == "baseline" and r["status"] == "excluded" for r in rows
            ),
            "pending_repairs": sum(
                r["arm"] == "baseline" and r["status"] in ("excluded", "pending_repair")
                for r in rows
            ),
            "blocked": sum(r["arm"] == "baseline" for r in blocked),
            "new_solve_output_tokens": sum(
                (r.get("solve_usage") or {}).get("n_output_tokens") or 0
                for r in complete
            ),
            "complete_chains": sum(
                all(
                    any(r["chain"] == c and r["task"] == t for r in complete)
                    for t in core.tasks(c)
                )
                for c in order
            ),
        }
    )
    paired = paired_metrics(rows, order)
    summary["paired_tasks"] = paired["n_tasks"]
    summary["paired_passed"] = paired["passed"]
    return rows, pending, blocked, summary


def run_stage(args):
    """Use the frozen solver, archiving incompatible history before fresh solving."""
    initialize_scheduler(args)
    if args.arm != "baseline":
        return core.run_stage(args)
    folder = core.stage_path(args.root, args.chain, args.arm, args.task)
    folder.parent.parent.mkdir(parents=True, exist_ok=True)
    with (folder.parent.parent / "worker.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = folder / "record.json"
        record = read_json(path)
        if record and record["status"] == "complete":
            return 0
        for previous in core.tasks(args.chain)[
            : core.tasks(args.chain).index(args.task)
        ]:
            if (
                read_json(
                    core.stage_path(args.root, args.chain, "baseline", previous)
                    / "record.json",
                    {},
                ).get("status")
                != "complete"
            ):
                raise RuntimeError("baseline predecessor still needs repair")
        if record and record["status"] == "excluded":
            if (folder / "launch.json").exists() or (folder / "trials").exists():
                raise RuntimeError(
                    "excluded baseline has a launched trial; review required"
                )
            archive = folder / "excluded_record.json"
            if archive.exists():
                raise RuntimeError("excluded baseline archive already exists")
            path.replace(archive)
    original_lookup = core.legacy_baseline

    def compatible_lookup(old_root, chain, task):
        archive = (
            core.stage_path(args.root, chain, "baseline", task) / "excluded_record.json"
        )
        if archive.exists():
            return None
        result = original_lookup(old_root, chain, task)
        if result and result["status"] == "excluded":
            atomic_json(archive, {**result, "task": task})
            return None
        return result

    with patch.object(core, "legacy_baseline", compatible_lookup):
        return core.run_stage(args)


def window_plan(order, rows, pending, active, check_chain, width=3):
    """Bound *lead*, not merely the number of currently active chains.

    Completed chains after the unfinished frontier still occupy window positions.
    Thus if A lags while B/C finish, D cannot be opened. Infrastructure-blocked
    chains are explicitly quarantined, never mistaken for completed chains.
    """
    records = {(r["chain"], r["arm"], r["task"]): r for r in rows}
    quarantined = sorted({r["chain"] for r in rows if r["status"] == "blocked"})
    settled = {
        chain: all(
            records.get((chain, arm, task), {}).get("status") == "complete"
            for arm in ARMS
            for task in core.tasks(chain)
        )
        for chain in order
    }
    active_keys = {(w["chain"], w["arm"]) for w in active.values()}
    active_chains = {c for c, _ in active_keys}
    # Finished RSI chains with baseline holes form a repair queue. They do not
    # push the current rolling window backwards and strand spare services.
    repair_chains = {
        c
        for c in order
        if not settled[c]
        and c not in quarantined
        and all(
            records.get((c, a, t), {}).get("status") == "complete"
            for a in ARMS[1:]
            for t in core.tasks(c)
        )
    }
    window, deferred = [], []
    for chain in order:
        if chain in quarantined or chain in repair_chains:
            continue
        if not window and settled[chain]:
            continue
        if not settled[chain] and chain not in active_chains:
            ok, why = check_chain(chain)
            if not ok:
                deferred.append({"chain": chain, "reason": why})
                continue
        window.append(chain)
        if len(window) == width:
            break
    candidates = [
        w
        for w in pending
        if w[0] in set(window) | repair_chains and w[:2] not in active_keys
    ]
    # Chain priority dominates; within a chain, serve the most lagging arm first.
    rank = {chain: index for index, chain in enumerate(order)}
    candidates.sort(
        key=lambda w: (
            w[0] not in repair_chains,
            rank[w[0]],
            core.tasks(w[0]).index(w[2]),
            ARMS.index(w[1]),
        )
    )
    return {
        "window": window,
        "frontier": window[0] if window else None,
        "preferred_chains": window[:2],
        "overflow_chain": window[2:3],
        "candidates": candidates,
        "baseline_repair_chains": sorted(repair_chains, key=rank.get),
        "deferred": deferred,
        "quarantined": quarantined,
        "preserved_active_outside_window": sorted(active_chains - set(window)),
        "window_limited": [
            w
            for w in pending
            if w[0] not in set(window) | repair_chains and w[0] not in quarantined
        ],
    }


def paired_metrics(rows, order):
    records = {(r["chain"], r["arm"], r["task"]): r for r in rows}
    matched, complete_chains = [], []
    for chain in order:
        chain_matches = []
        for task in core.tasks(chain):
            cohort = [records.get((chain, arm, task)) for arm in ARMS]
            if all(r and r["status"] == "complete" for r in cohort):
                chain_matches.append(
                    {
                        "chain": chain,
                        "task": task,
                        "rewards": {a: r["reward"] for a, r in zip(ARMS, cohort)},
                    }
                )
        matched.extend(chain_matches)
        if len(chain_matches) == len(core.tasks(chain)):
            complete_chains.append(chain)
    return {
        "tasks": matched,
        "n_tasks": len(matched),
        "passed": {arm: sum(r["rewards"][arm] > 0 for r in matched) for arm in ARMS},
        "pass_rate": {
            arm: (
                sum(r["rewards"][arm] > 0 for r in matched) / len(matched)
                if matched
                else None
            )
            for arm in ARMS
        },
        "complete_chains": complete_chains,
        "note": "Identical completed tasks only; incomplete cohorts are progress, not comparable scores.",
    }


def reap_workers(root, active):
    """Adopt still-running workers using PID start times; never re-solve a trial."""
    for port, worker in list(active.items()):
        identity = core.process_identity(worker["pid"])
        if identity is not None and identity == worker["identity"]:
            continue
        record = (
            core.stage_path(root, worker["chain"], worker["arm"], worker["task"])
            / "record.json"
        )
        if read_json(record, {}).get("status") == "excluded":
            atomic_json(
                record.parent / "repair_error.json",
                {
                    "status": "blocked",
                    "reason": "repair_worker_exited_without_replacing_exclusion",
                },
            )
        if not record.exists():
            child = read_json(record.parent / "child.json", {})
            child_identity = core.process_identity(child["pid"]) if child else None
            if child_identity is not None and child_identity == child["identity"]:
                continue
            recover = record.parent / "postprocess_recovery.json"
            if (
                list((record.parent / "trials").glob("*/result.json"))
                and not recover.exists()
            ):
                atomic_json(
                    recover,
                    {
                        "time": time.time(),
                        "reason": "resume_existing_result_without_resolving",
                    },
                )
            else:
                atomic_json(
                    record,
                    {
                        "status": "blocked",
                        "reason": "stage_worker_exited_without_record",
                    },
                )
        active.pop(port)


def supervise(args):
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    with (root / "supervisor.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        order = initialize_scheduler(args)
        active = read_json(root / "active.json", {})
        stopping = False
        readiness_cache = {}

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        def check_chain(chain):
            # Dispatch still freshly checks each task. Cache full-chain admission
            # checks briefly to avoid repeated Docker scans while services are busy.
            cached = readiness_cache.get(chain)
            if cached and time.time() < cached[0]:
                return cached[1]
            result = (True, None)
            for task in core.tasks(chain):
                ok, why = core.ready(task, args.old_root)
                if not ok:
                    result = (False, {"task": task, "reason": why})
                    break
            readiness_cache[chain] = (time.time() + (300 if result[0] else 30), result)
            return result

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            if (root / "PAUSE").exists():
                break
            reap_workers(root, active)
            gate = core.pilot_gate(root, args.pilot)
            selected = order if gate["passed"] else args.pilot
            adopted, adoption_waiting = adopt_baselines(
                root, args.old_root, selected, active
            )
            if adopted:
                with (root / "baseline-adoption.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps({"time": time.time(), "records": adopted}) + "\n"
                    )
            gate = core.pilot_gate(root, args.pilot)
            selected = order if gate["passed"] else args.pilot
            rows, pending, blocked, summary = campaign_status(root, selected)
            plan = window_plan(
                selected, rows, pending, active, check_chain, POLICY["window_chains"]
            )
            occupied = {int(w[0]) for w in core.live_chain_workers(args.old_root)}
            disk = core.docker_disk_status(core.PROJECT)
            endpoints = {str(port): core.endpoint_ok(port) for port in core.PORTS}
            waiting = []
            active_keys = {(w["chain"], w["arm"]) for w in active.values()}
            for port in core.PORTS:
                if stopping or (root / "PAUSE").exists():
                    break
                if (
                    not disk["ok"]
                    or str(port) in active
                    or port in occupied
                    or not endpoints[str(port)]
                ):
                    continue
                chosen = None
                for candidate in plan["candidates"]:
                    chain, arm, task = candidate
                    if (chain, arm) in active_keys:
                        continue
                    ok, why = core.ready(task, args.old_root)
                    if not ok:
                        waiting.append(
                            {"chain": chain, "arm": arm, "task": task, "reason": why}
                        )
                        continue
                    chosen = candidate
                    break
                if chosen is None:
                    continue
                chain, arm, task = chosen
                log_path = root / "logs" / f"{chain}-{arm}-{task}.log"
                log_path.parent.mkdir(exist_ok=True)
                command = [
                    sys.executable,
                    "-u",
                    "-m",
                    "rsi_harness.clean_scheduler",
                    "--stage",
                    "--root",
                    str(root),
                    "--old-root",
                    str(args.old_root),
                    "--chain",
                    chain,
                    "--arm",
                    arm,
                    "--task",
                    task,
                    "--port",
                    str(port),
                    "--pilot",
                    *args.pilot,
                ]
                with log_path.open("ab", buffering=0) as stream:
                    process = subprocess.Popen(
                        command,
                        cwd=core.PROJECT,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                active[str(port)] = {
                    "pid": process.pid,
                    "identity": core.process_identity(process.pid),
                    "chain": chain,
                    "arm": arm,
                    "task": task,
                    "started": time.time(),
                }
                active_keys.add((chain, arm))
                atomic_json(root / "active.json", active)
            terminal = None
            if not active and not pending:
                terminal = (
                    "complete" if gate["passed"] and not blocked else "needs_review"
                )
            elif (
                not active
                and pending
                and all(c in plan["quarantined"] for c, _, _ in pending)
            ):
                terminal = "needs_review"
            atomic_json(root / "active.json", active)
            atomic_json(
                root / "status.json",
                {
                    "time": time.time(),
                    "supervisor_pid": os.getpid(),
                    "phase": "full" if gate["passed"] else "pilot",
                    "pilot_gate": gate,
                    "summary": summary,
                    "paired": paired_metrics(rows, selected),
                    "active": active,
                    "waiting": waiting,
                    "blocked": blocked,
                    "disk": disk,
                    "endpoints": endpoints,
                    "old_workers_draining_ports": sorted(occupied),
                    "terminal": terminal,
                    "scheduling": {
                        "policy": POLICY,
                        **plan,
                        "baseline_adoption_waiting": adoption_waiting,
                    },
                },
            )
            if terminal:
                return 0
            time.sleep(20)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--old-root", type=Path, default=core.OLD_ROOT)
    parser.add_argument("--migrate-baseline-repairs", action="store_true")
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--chain")
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--task")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--pilot", nargs="+", default=["break-filter-js-from-html", "chess-best-move"]
    )
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.old_root = args.old_root.resolve()
    if args.migrate_baseline_repairs:
        with (args.root / "supervisor.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            initialize_scheduler(args)
        return 0
    if args.stage:
        if not all((args.chain, args.arm, args.task, args.port)):
            parser.error("stage requires chain, arm, task and port")
        return run_stage(args)
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
