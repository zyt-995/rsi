"""Read-only historical controls + an isolated, two-worker meta-skill campaign."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import clean_campaign as core
from . import meta_skill as method
from .clean_rsi import SOLVE, digest, initialize, read_json
from .terminal_infra import atomic_json, file_hash, fingerprint, readiness

PROJECT, DATASET = core.PROJECT, core.DATASET
CONTROL = PROJECT / "runs/clean-rsi-20260923-qwen38-27b"
PORTS = (8040, 8041)
ARM = "meta-skill-rsi"


def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def stage_path(root, chain, task):
    return root / chain / ARM / "stages" / task


def code_snapshot():
    paths = set(PROJECT.joinpath("rsi_harness").rglob("*.py"))
    for base in (DATASET.parent / "harbor/src/harbor",
                 DATASET.parent / "harbor_EvoMem"):
        paths.update(p for p in base.rglob("*") if p.is_file()
                     and p.suffix in (".py", ".j2", ".jinja", ".jinja2")
                     and "__pycache__" not in p.parts)
    paths.update(PROJECT / p for p in ("scripts/docker-env.sh",
                 "scripts/preflight-terminal-environments.py", "scripts/runc-rootless.sh"))
    paths.update(DATASET.parent / f"harbor_EvoMem/scripts/terminus2_llm_{p}.env" for p in PORTS)
    return {str(p): file_hash(p) for p in sorted(paths)}


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    with exclusive(root / "prepare.lock"):
        if (root / "manifest.json").exists():
            return read_json(root / "manifest.json")
        original = read_json(CONTROL / "scheduler_manifest.json")
        order, task_order, controls, issues = [], {}, {}, []
        for chain in original["chain_order"]:
            records = {(arm, task): read_json(CONTROL / chain / arm / "stages" / task / "record.json", {})
                       for arm in ("baseline", "skill-rsi") for task in original["task_order"][chain]}
            if not all(r.get("status") == "complete" for r in records.values()):
                continue
            order.append(chain)
            task_order[chain] = original["task_order"][chain]
            for (arm, task), record in records.items():
                result = Path(record["result"])
                why = core.baseline_compatibility(result, task)
                if why:
                    issues.append({"chain": chain, "arm": arm, "task": task, "reasons": why})
                paths = [CONTROL / chain / arm / "stages" / task / "record.json", result,
                         result.parent / "verifier/ctrf.json", result.parent / "agent/trajectory.json"]
                controls[f"{arm}/{task}"] = {"chain": chain, "task": task, "arm": arm,
                      "reward": record["reward"], "result": str(result), "record": record,
                      "hashes": {str(p): file_hash(p) for p in paths}}
        if len(order) != 21 or sum(map(len, task_order.values())) != 103:
            raise RuntimeError("historical cohort changed; requires explicit cohort review")
        atomic_json(root / "control_audit.json", {"issues": issues, "controls": controls})
        if issues:
            raise RuntimeError("historical controls incompatible; see control_audit.json")
        from harbor.models.task.task import Task
        tasks = [t for c in order for t in task_order[c]]
        manifest = {"schema": method.SCHEMA, "created": time.time(), "method": ARM,
                    "model": core.MODEL, "benchmark": "Terminal-Bench-Evo",
                    "controls": str(CONTROL), "chain_order": order, "task_order": task_order,
                    "solve": SOLVE, "verifier_timeout": 600, "improve": method.BUDGET,
                    "ports": list(PORTS), "concurrency": 2,
                    "solver_adapter_arm": "skill-rsi", "retrieval": {"top_k": 4, "chars": 12000},
                    "reset": "empty library at every chain boundary; no cross-chain learning",
                    "cohort_selection": "whole chains complete in BOTH controls; no score selection",
                    "interpretation": "observed development cohort, NOT blind or equal-total-compute",
                    "task_checksums": {t: Task(DATASET / t).checksum for t in tasks},
                    "environment_fingerprints": {t: fingerprint(DATASET / t) for t in tasks},
                    "control_audit_sha256": file_hash(root / "control_audit.json")}
        atomic_json(root / "manifest.json", manifest)
        return manifest


def seal(root, acceptance):
    report = read_json(acceptance / "acceptance.json", {})
    if not report.get("passed") or report.get("formal") is not False:
        raise RuntimeError("independent real-model acceptance must pass first")
    current = code_snapshot()
    if report.get("code_sha256") != digest(current):
        raise RuntimeError("code changed after acceptance")
    value = {"manifest_sha256": file_hash(root / "manifest.json"), "code": current,
             "acceptance": str(acceptance / "acceptance.json"),
             "acceptance_sha256": file_hash(acceptance / "acceptance.json"), "time": time.time()}
    if (root / "seal.json").exists():
        verify(root)
        return
    atomic_json(root / "seal.json", value)


def verify(root):
    seal_value = read_json(root / "seal.json")
    if not seal_value or seal_value["manifest_sha256"] != file_hash(root / "manifest.json"):
        raise RuntimeError("missing seal or changed protocol")
    if seal_value["code"] != code_snapshot():
        raise RuntimeError("frozen source changed; do not mix protocol versions")
    if file_hash(Path(seal_value["acceptance"])) != seal_value["acceptance_sha256"]:
        raise RuntimeError("acceptance changed")
    manifest = read_json(root / "manifest.json")
    if file_hash(root / "control_audit.json") != manifest["control_audit_sha256"]:
        raise RuntimeError("control audit changed")
    return manifest


def verify_task(root, manifest, task):
    from harbor.models.task.task import Task
    if Task(DATASET / task).checksum != manifest["task_checksums"][task]:
        raise RuntimeError("task changed:" + task)
    if fingerprint(DATASET / task) != manifest["environment_fingerprints"][task]:
        raise RuntimeError("environment changed:" + task)
    audit = read_json(root / "control_audit.json")["controls"]
    for arm in ("baseline", "skill-rsi"):
        for path, expected in audit[f"{arm}/{task}"]["hashes"].items():
            if file_hash(Path(path)) != expected:
                raise RuntimeError("historical control changed:" + path)


def run_stage(root, chain, task, port):
    manifest = verify(root)
    if port not in manifest["ports"] or task not in manifest["task_order"][chain]:
        raise ValueError("work outside frozen manifest")
    work = root / chain / ARM
    with exclusive(work / "worker.lock"), exclusive(PROJECT / "runs" / f"meta-port-{port}.lock"):
        folder = stage_path(root, chain, task)
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "record.json").exists():
            return
        series = manifest["task_order"][chain]
        for previous in series[:series.index(task)]:
            if read_json(stage_path(root, chain, previous) / "record.json", {}).get("status") != "complete":
                raise RuntimeError("chain predecessor not complete")
        verify_task(root, manifest, task)
        ok, reason = core.ready(task, root)
        if not ok:
            raise RuntimeError("preflight admission failed:" + str(reason))
        evidence = readiness(root, task)
        store = work / "state"
        before = initialize(store, "skill-rsi")
        results = list((folder / "trials").glob("*/result.json"))
        launch = read_json(folder / "launch.json")
        if not results:
            if launch:
                raise RuntimeError("interrupted_trial_not_repeated")
            command = core.harbor_command(DATASET / task, folder / "trials", "skill-rsi", store)
            atomic_json(folder / "launch.json", {"task": task, "method": ARM,
                        "solver_adapter_arm": "skill-rsi", "port": port,
                        "command": command, "before": before,
                        "started": time.time(), "readiness": evidence})
            with (folder / "worker.log").open("ab", buffering=0) as stream:
                process = subprocess.Popen(command, env=core.runner_environment(port), cwd=PROJECT,
                                           stdout=stream, stderr=subprocess.STDOUT)
                atomic_json(folder / "child.json", {"pid": process.pid,
                            "identity": core.process_identity(process.pid)})
                try:
                    process.wait(timeout=10800)
                except subprocess.TimeoutExpired:
                    # Harbor agent+verifier caps are shorter. Do not kill an unknown container
                    # or silently retry; the operator gets exact pid/identity for investigation.
                    raise RuntimeError("harbor_outer_watchdog; child retained for inspection")
            results = list((folder / "trials").glob("*/result.json"))
        if len(results) != 1:
            raise RuntimeError("missing_or_ambiguous_trial")
        path = results[0]
        valid, reason = core.scored_result(path)
        if not valid:
            raise RuntimeError("invalid_result:" + str(reason))
        value = read_json(path)
        runtime = read_json(path.parent / "agent/rsi_end.json", {})
        start = read_json(path.parent / "agent/rsi_start.json", {})
        launch = read_json(folder / "launch.json")
        if start.get("version") != launch["before"]:
            raise RuntimeError("solver_did_not_consume_expected_version")
        # An agent wall-time limit is a valid outcome; metadata may then be absent.
        if runtime.get("model_calls", 0) > SOLVE["max_model_calls"] or runtime.get("commands", 0) > SOLVE["max_commands"]:
            raise RuntimeError("solver_budget_violation")
        reward = value["verifier_result"]["rewards"]["reward"]
        # Save valid score BEFORE updating. Resume can only finish its update, never re-solve.
        atomic_json(folder / "scored.json", {"reward": reward, "result": str(path), "time": time.time()})
        improvement = None
        if task != series[-1]:
            improvement = method.improve(store, task, (DATASET / task / "instruction.md").read_text(),
                                         path.parent, reward, f"http://127.0.0.1:{port}/v1", core.MODEL)
        atomic_json(folder / "record.json", {"status": "complete", "task": task, "arm": ARM,
                    "chain": chain, "result": str(path), "reward": reward,
                    "before": start.get("version"), "runtime": runtime,
                    "solve_usage": {k: (value.get("agent_result") or {}).get(k)
                                    for k in ("n_input_tokens", "n_output_tokens", "n_cache_tokens")},
                    "solve_seconds": (path.stat().st_mtime - launch["started"]),
                    "improvement": improvement, "last_stage_no_improvement": task == series[-1],
                    "readiness": evidence, "finished": time.time()})


def scan(root, manifest):
    completed, candidates, blocked, full = [], [], [], []
    for chain in manifest["chain_order"]:
        series = manifest["task_order"][chain]
        prefix = 0
        for task in series:
            record = read_json(stage_path(root, chain, task) / "record.json", {})
            if record.get("status") == "complete":
                completed.append(record)
                prefix += 1
            elif record:
                blocked.append({"chain": chain, "task": task, "record": record})
                break
            else:
                candidates.append({"chain": chain, "task": task, "prefix": prefix})
                break
        if prefix == len(series):
            full.append(chain)
    # Finish a started chain before opening another; no barriers on other chains.
    candidates.sort(key=lambda r: (r["prefix"] == 0, manifest["chain_order"].index(r["chain"])))
    return completed, candidates, blocked, full


def metrics(root, manifest=None):
    manifest = manifest or read_json(root / "manifest.json")
    rows, pending, blocked, full = scan(root, manifest)
    controls = read_json(root / "control_audit.json")["controls"]
    arms = ("baseline", "skill-rsi", ARM)
    def matched(selected):
        rewards = {arm: [r["reward"] if arm == ARM else controls[f"{arm}/{r['task']}"]["reward"]
                         for r in selected] for arm in arms}
        return {"tasks": len(selected), "arms": {arm: {"reward_sum": sum(v),
                 "mean_reward": sum(v) / len(v) if v else None,
                 "passed": sum(x == 1 for x in v)} for arm, v in rewards.items()},
                "paired_deltas": {arm: {"improved": [r["task"] for r, a, b in zip(selected, rewards[ARM], rewards[arm]) if a > b],
                                        "regressed": [r["task"] for r, a, b in zip(selected, rewards[ARM], rewards[arm]) if a < b]}
                                  for arm in arms[:-1]}}
    updates = [r["improvement"] for r in rows if r.get("improvement")]
    control_costs = {}
    for arm in arms[:-1]:
        records = [controls[f"{arm}/{r['task']}"] for r in rows]
        trials = [read_json(Path(r["result"])) for r in records]
        improvements = [r["record"].get("improvement") for r in records if r.get("record", {}).get("improvement")]
        control_costs[arm] = {
            "matched_tasks": len(records),
            "solve_tokens_known": {key: sum((v.get("agent_result") or {}).get(key) or 0 for v in trials)
                                   for key in ("n_input_tokens", "n_output_tokens")},
            "solve_usage_missing_tasks": sum((v.get("agent_result") or {}).get("n_input_tokens") is None for v in trials),
            "update_tokens_known": {key: sum((u.get("usage") or {}).get(key, 0) for u in improvements)
                                    for key in ("prompt_tokens", "completion_tokens")},
            "update_usage_missing": sum(u.get("usage") is None for u in improvements),
            "note": "observed usage on matched tasks; terminal-stage update opportunities differ for incomplete chains"}
    return {"time": time.time(), "complete_tasks": len(rows),
            "total_tasks": sum(map(len, manifest["task_order"].values())),
            "complete_chains": full, "total_chains": len(manifest["chain_order"]),
            "matched_tasks": matched(rows), "matched_full_chains": matched([r for r in rows if r["chain"] in full]),
            "chain_macro_mean": {arm: (sum(sum(r["reward"] if arm == ARM else controls[f"{arm}/{r['task']}"]["reward"]
                                        for r in rows if r["chain"] == c) / len(manifest["task_order"][c])
                                        for c in full) / len(full) if full else None) for arm in arms},
            "cost": {"solve_seconds": sum(r.get("solve_seconds", 0) for r in rows),
                     "solve_calls_known": sum((r.get("runtime") or {}).get("model_calls", 0) for r in rows),
                     "solve_tokens_known": {k: sum((r.get("solve_usage") or {}).get(k) or 0 for r in rows)
                                            for k in ("n_input_tokens", "n_output_tokens")},
                     "solve_usage_missing_tasks": sum((r.get("solve_usage") or {}).get("n_input_tokens") is None for r in rows),
                     "solve_call_counts_missing_tasks": sum("model_calls" not in (r.get("runtime") or {}) for r in rows),
                     "update_calls": sum(u["accounting"]["calls"] for u in updates),
                     "update_seconds": sum(u["seconds"] for u in updates),
                     "update_tokens_known": {k: sum(u["accounting"]["known_tokens"][k] for u in updates)
                                             for k in ("prompt_tokens", "completion_tokens")},
                     "updates_accepted": sum(u["accepted"] for u in updates),
                     "updates_total": len(updates),
                     "update_usage_missing": sum(not u["accounting"]["usage_complete"] for u in updates),
                     "equal_total_compute": False},
            "matched_control_costs": control_costs,
            "blocked": blocked, "pending_chain_count": len(pending),
            "caveat": "descriptive observed-cohort comparison; partial chains != complete chains"}


def alive(worker):
    return bool(worker.get("identity")) and core.process_identity(worker["pid"]) == worker["identity"]


def endpoint_idle(port):
    """Do not enqueue behind unrecognized work on a shared inference service."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/metrics", timeout=3) as response:
            lines = response.read().decode().splitlines()
        gauges = [float(s.rsplit(" ", 1)[-1]) for s in lines if s.startswith(
            ("vllm:num_requests_running{", "vllm:num_requests_waiting{"))]
        return bool(gauges) and all(v == 0 for v in gauges)
    except (OSError, ValueError):
        return False


def run(root):
    manifest = verify(root)
    with exclusive(root / "scheduler.lock"):
        active = read_json(root / "active.json", {})
        children, stopping = {}, False
        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            verify(root)
            for port, worker in list(active.items()):
                if port in children:
                    children[port].poll()
                if alive(worker):
                    continue
                path = stage_path(root, worker["chain"], worker["task"])
                if not (path / "record.json").exists():
                    atomic_json(path / "record.json", {"status": "blocked", "task": worker["task"],
                                "reason": "worker_interrupted; inspect valid result before recovery"})
                del active[port]
            rows, candidates, blocked, full = scan(root, manifest)
            disk = core.docker_disk_status(PROJECT)
            old_active = read_json(CONTROL / "active.json", {})
            occupied = {p for p, w in old_active.items() if alive(w)}
            occupied.update(str(w[0]) for w in core.live_chain_workers(core.OLD_ROOT))
            active_chains = {w["chain"] for w in active.values()}
            waiting = []
            if not stopping and not (root / "PAUSE").exists() and disk["ok"]:
                for port in map(str, manifest["ports"]):
                    if (port in active or port in occupied or not core.endpoint_ok(int(port))
                            or not endpoint_idle(int(port))):
                        continue
                    for candidate in candidates:
                        chain, task = candidate["chain"], candidate["task"]
                        if chain in active_chains:
                            continue
                        # Full-chain admission avoids stranding freshly opened chains.
                        check_tasks = manifest["task_order"][chain] if candidate["prefix"] == 0 else [task]
                        failed = next(((t, why) for t in check_tasks
                                       for ok, why in [core.ready(t, root)] if not ok), None)
                        if failed:
                            waiting.append({"chain": chain, "task": failed[0], "reason": failed[1]})
                            continue
                        verify_task(root, manifest, task)
                        log = root / "logs" / f"{task}.log"
                        log.parent.mkdir(exist_ok=True)
                        command = [sys.executable, "-u", "-m", "rsi_harness.meta_skill_campaign",
                                   "--root", str(root), "--stage", "--chain", chain,
                                   "--task", task, "--port", port]
                        with log.open("ab", buffering=0) as stream:
                            process = subprocess.Popen(command, cwd=PROJECT, stdout=stream,
                                                       stderr=subprocess.STDOUT, start_new_session=True)
                        children[port] = process
                        active[port] = {"pid": process.pid, "identity": core.process_identity(process.pid),
                                        "chain": chain, "task": task, "started": time.time()}
                        active_chains.add(chain)
                        atomic_json(root / "active.json", active)
                        break
            terminal = "complete" if len(full) == len(manifest["chain_order"]) else None
            if not active and (stopping or (root / "PAUSE").exists()):
                terminal = "paused"
            if not active and not candidates and blocked:
                terminal = "needs_review"
            atomic_json(root / "active.json", active)
            report = metrics(root, manifest)
            atomic_json(root / "metrics.json", report)
            atomic_json(root / "status.json", {"time": time.time(), "pid": os.getpid(),
                        "active": active, "waiting": waiting, "blocked": blocked,
                        "disk": disk, "occupied_old_ports": sorted(occupied),
                        "port_conflicts": sorted(set(active) & occupied),
                        "complete_tasks": len(rows), "complete_chains": full, "terminal": terminal})
            # Persistent half-hour snapshots are diagnostics, not automatic Codex wakeups.
            bucket = int(time.time() // 1800)
            snapshot = root / "monitor" / f"{bucket}.json"
            if not snapshot.exists():
                atomic_json(snapshot, {"status": read_json(root / "status.json"), "metrics": report})
            if terminal:
                return
            time.sleep(20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    for name in ("prepare", "preflight", "seal", "run", "stage", "report"):
        group.add_argument("--" + name, action="store_true")
    parser.add_argument("--acceptance", type=Path)
    parser.add_argument("--chain")
    parser.add_argument("--task")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        m = prepare(root)
        print(json.dumps({"chains": len(m["chain_order"]), "tasks": sum(map(len, m["task_order"].values()))}))
    elif args.preflight:
        m = read_json(root / "manifest.json")
        command = [sys.executable, "-u", str(PROJECT / "scripts/preflight-terminal-environments.py"),
                   "--root", str(root), "--jobs", "2", "--once"]
        for chain in m["chain_order"]:
            for task in m["task_order"][chain]:
                command += ["--task", task]
        return subprocess.call(command, cwd=PROJECT)
    elif args.seal:
        seal(root, args.acceptance.resolve())
    elif args.run:
        run(root)
    elif args.report:
        print(json.dumps(metrics(root), ensure_ascii=False, indent=2))
    elif args.stage:
        try:
            run_stage(root, args.chain, args.task, args.port)
        except Exception as exc:
            folder = stage_path(root, args.chain, args.task)
            if not (folder / "record.json").exists():
                atomic_json(folder / "record.json", {"status": "blocked", "task": args.task,
                            "reason": f"{type(exc).__name__}:{exc}"})
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
