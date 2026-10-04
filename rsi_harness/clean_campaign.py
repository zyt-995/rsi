"""Resumable paired campaign, with a complete-chain pilot gate before expansion."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .clean_rsi import (
    ARMS,
    IMPROVE,
    SCHEMA,
    SOLVE,
    atomic_json,
    digest,
    improve,
    initialize,
    read_json,
)
from .terminal_infra import (
    DATASET,
    PROJECT,
    docker_disk_status,
    live_chain_workers,
    readiness,
)

OLD_ROOT = PROJECT / "runs/full-20260922-qwen38-27b/terminal"
MODEL = "Qwen/Qwen3.8-27B"
HARBOR = PROJECT / ".venv-terminal/bin/harbor"
PORTS = (8040, 8041, 8042, 8044)
AGENT_LIMIT_ERRORS = {"AgentTimeoutError", "OutputLengthExceededError"}


def tasks(chain):
    return (
        [chain]
        + [
            p.name
            for p in sorted(DATASET.glob(chain + "-EVO-*"))
            if (p / "task.toml").exists()
        ]
        + [
            p.name
            for p in sorted(DATASET.glob(chain + "-v-*"))
            if (p / "task.toml").exists()
        ]
    )


def chains():
    return sorted(
        p.name
        for p in DATASET.iterdir()
        if p.is_dir()
        and "-EVO-" not in p.name
        and (p / "task.toml").exists()
        and (p / "instruction.md").exists()
    )


def missing_assets(task):
    bad = []
    for base in (task / "environment", task / "tests"):
        for p in base.rglob("*"):
            if p.is_file() and p.stat().st_size < 1024:
                with p.open("rb") as stream:
                    if stream.read(100).startswith(
                        b"version https://git-lfs.github.com/spec/v1"
                    ):
                        bad.append(str(p.relative_to(task)))
    return bad


def ready(task, old_root):
    ticket = readiness(old_root, task)
    if ticket.get("status") != "ready":
        return False, "preflight:" + str(ticket.get("status"))
    bad = missing_assets(DATASET / task)
    if bad:
        return False, "unmaterialized_lfs:" + ",".join(bad)
    result = subprocess.run(
        ["docker", "image", "inspect", ticket["image"], "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return (
        (True, None)
        if result.returncode == 0 and result.stdout.strip() == ticket["image_id"]
        else (False, "image_changed")
    )


def scored_result(path):
    value = read_json(path, {})
    exception = value.get("exception_info")
    if exception and exception.get("exception_type") not in AGENT_LIMIT_ERRORS:
        return False, "trial_exception"
    score = (value.get("verifier_result") or {}).get("rewards", {}).get("reward")
    if type(score) not in (int, float):
        return False, "missing_reward"
    report = read_json(path.parent / "verifier/ctrf.json", {})
    summary = report.get("results", {}).get("summary", {})
    if (
        type(summary.get("tests")) is not int
        or summary["tests"] <= 0
        or summary.get("other", 0)
        or summary.get("pending", 0)
    ):
        return False, "verifier_incomplete"
    stdout = path.parent / "verifier/test-stdout.txt"
    text = stdout.read_text(errors="replace") if stdout.exists() else ""
    for marker in (
        "curl: command not found",
        "uvx: command not found",
        "No space left on device",
        "Could not resolve host",
        "Connection refused",
        "Unable to locate package",
        "Failed to establish a new connection",
        "Network is unreachable",
    ):
        if marker in text:
            return False, "verifier_infrastructure:" + marker
    return True, None


def baseline_compatibility(path, task):
    valid, reason = scored_result(path)
    if not valid:
        return [reason]
    d = read_json(path)
    cfg = d["config"]["agent"]
    kw = cfg.get("kwargs", {})
    reasons = []
    if cfg.get("model_name") != "openai/" + MODEL:
        reasons.append("model")
    expected = {
        "max_turns": 15,
        "max_output_retries": 1,
        "llm_call_kwargs": {"max_tokens": 32768, "timeout": 2700},
    }
    for key, value in expected.items():
        if kw.get(key) != value:
            reasons.append(key)
    if cfg.get("override_timeout_sec") != SOLVE["timeout"]:
        reasons.append("agent_timeout")
    if d["config"].get("verifier", {}).get("override_timeout_sec") != 600:
        reasons.append("verifier_timeout")
    metadata = (d.get("agent_result") or {}).get("metadata", {})
    calls = len(metadata.get("api_request_times_msec", []))
    if (
        not calls
        or calls > SOLVE["max_model_calls"]
        or metadata.get("summarization_count", 0)
    ):
        reasons.append("model_call_budget")
    trace = read_json(path.parent / "agent/trajectory.json", {})
    commands = sum(len(step.get("tool_calls") or []) for step in trace.get("steps", []))
    if commands > SOLVE["max_commands"]:
        reasons.append("command_budget")
    from harbor.models.task.task import Task

    if d.get("task_checksum") != Task(DATASET / task).checksum:
        reasons.append("task_checksum")
    return reasons


def legacy_baseline(old_root, chain, task):
    candidates = []
    for path in (old_root / chain / "baseline").glob("*/result.json"):
        d = read_json(path)
        if (
            d.get("task_name") == task
            and (
                not d.get("exception_info")
                or d["exception_info"].get("exception_type") in AGENT_LIMIT_ERRORS
            )
            and (d.get("verifier_result") or {}).get("rewards", {}).get("reward")
            is not None
        ):
            candidates.append(path)
    rejected = []
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime):
        reasons = baseline_compatibility(path, task)
        if not reasons:
            return {
                "status": "complete",
                "source": "legacy_reuse",
                "result": str(path),
                "reward": read_json(path)["verifier_result"]["rewards"]["reward"],
            }
        rejected.append({"result": str(path), "reasons": reasons})
    if rejected:
        return {
            "status": "excluded",
            "source": "legacy_incompatible",
            "audit": rejected,
        }
    return None


def endpoint_ok(port):
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/models",
            headers={"Authorization": "Bearer EMPTY"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            return response.status == 200 and any(
                model.get("id") == MODEL
                for model in json.load(response).get("data", [])
            )
    except (OSError, ValueError):
        return False


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except OSError:
        return None


def runner_environment(port):
    env = os.environ.copy()
    env.update(
        HARBOR_EVOMEM_LLM_ENV=str(
            DATASET.parent / f"harbor_EvoMem/scripts/terminus2_llm_{port}.env"
        ),
        PYTHONPATH=str(PROJECT) + os.pathsep + env.get("PYTHONPATH", ""),
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        HARBOR_FORMAL_REQUIRE_CLEAN_VERIFIER="1",
        HARBOR_TMUX_BUNDLE=str(
            PROJECT / ".cache/tmux/tmux-runtime-singleuid-amd64.tar.gz"
        ),
        HARBOR_VERIFIER_BUNDLE=str(
            PROJECT / ".cache/tmux/verifier-runtime-singleuid-amd64.tar.gz"
        ),
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    return env


def harbor_command(task_path, trials, arm, store):
    command = [
        str(HARBOR),
        "trials",
        "start",
        "--path",
        str(task_path),
        "--trials-dir",
        str(trials),
        "--agent-import-path",
        "rsi_harness.clean_terminal_agent:CleanTerminalAgent",
        "--model",
        "openai/" + MODEL,
        "--agent-timeout",
        str(SOLVE["timeout"]),
        "--verifier-timeout",
        "600",
    ]
    kwargs = {
        "rsi_arm": arm,
        "rsi_store": str(store),
        "record_terminal_session": False,
        "max_turns": SOLVE["max_turns"],
        "suppress_max_turns_warning": True,
        "max_output_retries": SOLVE["max_output_retries"],
        "llm_call_kwargs": {"max_tokens": SOLVE["max_tokens"], "timeout": 2700},
    }
    for key, value in kwargs.items():
        command += [
            "--agent-kwarg",
            key + "=" + (value if isinstance(value, str) else json.dumps(value)),
        ]
    environment = {
        "HTTP_PROXY": "http://10.0.2.2:17897",
        "HTTPS_PROXY": "http://10.0.2.2:17897",
        "http_proxy": "http://10.0.2.2:17897",
        "https_proxy": "http://10.0.2.2:17897",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for key, value in environment.items():
        command += ["--ae", f"{key}={value}", "--ve", f"{key}={value}"]
    for key, value in {"GIT_PAGER": "cat", "PAGER": "cat"}.items():
        command += ["--ae", f"{key}={value}"]
    for key, value in {
        "TAR_OPTIONS": "--no-same-owner",
        "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "UV_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
    }.items():
        command += ["--ve", f"{key}={value}"]
    return command


def stage_path(root, chain, arm, task):
    return root / chain / arm / "stages" / task


def run_stage(args):
    root, chain, arm, task = args.root, args.chain, args.arm, args.task
    work = root / chain / arm
    work.mkdir(parents=True, exist_ok=True)
    lock = (work / "worker.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    folder = stage_path(root, chain, arm, task)
    folder.mkdir(parents=True, exist_ok=True)
    record_path = folder / "record.json"
    if record_path.exists():
        return 0
    series = tasks(chain)
    for previous in series[: series.index(task)]:
        record = read_json(stage_path(root, chain, arm, previous) / "record.json", {})
        if record.get("status") not in ("complete", "excluded"):
            raise RuntimeError("chain predecessor is not complete")
    available, reason = ready(task, args.old_root)
    if not available:
        atomic_json(record_path, {"status": "blocked", "reason": reason})
        return 2
    evidence = readiness(args.old_root, task)
    if arm == "baseline":
        reused = legacy_baseline(args.old_root, chain, task)
        if reused:
            atomic_json(record_path, {**reused, "task": task, "readiness": evidence})
            return 0
    store = work / "state"
    before = initialize(store, arm) if arm != "baseline" else None
    launch_path = folder / "launch.json"
    results = list((folder / "trials").glob("*/result.json"))
    if not results:
        if launch_path.exists():
            atomic_json(
                record_path,
                {"status": "blocked", "reason": "interrupted_trial_not_repeated"},
            )
            return 2
        command = harbor_command(DATASET / task, folder / "trials", arm, store)
        atomic_json(
            launch_path,
            {
                "task": task,
                "arm": arm,
                "command": command,
                "started": time.time(),
                "before": before,
                "readiness": evidence,
            },
        )
        with (folder / "worker.log").open("ab", buffering=0) as stream:
            process = subprocess.Popen(
                command,
                env=runner_environment(args.port),
                cwd=PROJECT,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            atomic_json(
                folder / "child.json",
                {"pid": process.pid, "identity": process_identity(process.pid)},
            )
            process.wait()
        results = list((folder / "trials").glob("*/result.json"))
    if len(results) != 1:
        atomic_json(
            record_path, {"status": "blocked", "reason": "missing_or_ambiguous_trial"}
        )
        return 2
    path = results[0]
    valid, reason = scored_result(path)
    if not valid:
        atomic_json(
            record_path, {"status": "blocked", "reason": reason, "result": str(path)}
        )
        return 2
    result = read_json(path)
    before = read_json(path.parent / "agent/rsi_start.json", {}).get("version", before)
    score = result["verifier_result"]["rewards"]["reward"]
    improvement = None
    if arm != "baseline" and task != series[-1]:
        from harbor.models.task.task import Task

        improvement = improve(
            store,
            arm,
            task,
            Task(DATASET / task).instruction,
            path.parent,
            score,
            f"http://127.0.0.1:{args.port}/v1",
            MODEL,
        )
    record = {
        "status": "complete",
        "source": "new",
        "task": task,
        "arm": arm,
        "result": str(path),
        "reward": score,
        "before": before,
        "runtime": read_json(path.parent / "agent/rsi_end.json", {}),
        "agent_limit_exception": result.get("exception_info"),
        "solve_usage": {
            k: (result.get("agent_result") or {}).get(k)
            for k in ("n_input_tokens", "n_output_tokens", "n_cache_tokens")
        },
        "improvement": improvement,
        "last_stage_no_improvement": task == series[-1],
        "readiness": evidence,
        "finished": time.time(),
    }
    atomic_json(record_path, record)
    return 0


def status(root, selected_chains):
    rows, next_work, blocked = [], [], []
    for chain in selected_chains:
        for arm in ARMS:
            first_missing = None
            for task in tasks(chain):
                record = read_json(stage_path(root, chain, arm, task) / "record.json")
                if record is None:
                    first_missing = task
                    break
                rows.append({"chain": chain, "arm": arm, "task": task, **record})
                if record["status"] == "blocked":
                    blocked.append({"chain": chain, "arm": arm, "task": task, **record})
                    break
            if first_missing:
                next_work.append((chain, arm, first_missing))
    summary = {}
    for arm in ARMS:
        complete = [r for r in rows if r["arm"] == arm and r["status"] == "complete"]
        summary[arm] = {
            "completed": len(complete),
            "passed": sum(r.get("reward", 0) > 0 for r in complete),
            "excluded": sum(
                r["arm"] == arm and r["status"] == "excluded" for r in rows
            ),
            "blocked": sum(r["arm"] == arm for r in blocked),
            "improvements": sum(
                bool((r.get("improvement") or {}).get("accepted")) for r in complete
            ),
            "improvement_rejected": sum(
                (r.get("improvement") or {}).get("status") == "rejected"
                for r in complete
            ),
            "improvement_no_change": sum(
                (r.get("improvement") or {}).get("status") == "no_change"
                for r in complete
            ),
            "controller_runtime_errors": sum(
                bool((r.get("runtime") or {}).get("controller_runtime_error"))
                for r in complete
            ),
            "new_solve_output_tokens": sum(
                (r.get("solve_usage") or {}).get("n_output_tokens") or 0
                for r in complete
            ),
            "improvement_output_tokens": sum(
                (
                    ((r.get("improvement") or {}).get("usage") or {}).get(
                        "completion_tokens"
                    )
                    or 0
                )
                for r in complete
            ),
            "complete_chains": sum(
                all(
                    any(
                        row["chain"] == chain and row["task"] == task
                        for row in complete
                    )
                    for task in tasks(chain)
                )
                for chain in selected_chains
            ),
        }
    summary["expected_tasks_per_arm"] = sum(len(tasks(c)) for c in selected_chains)
    summary["expected_chains_per_arm"] = len(selected_chains)
    paired = []
    for chain in selected_chains:
        for task in tasks(chain):
            matched = [
                r
                for r in rows
                if r["chain"] == chain
                and r["task"] == task
                and r["status"] == "complete"
            ]
            if len(matched) == len(ARMS):
                paired.append(matched)
    summary["paired_tasks"] = len(paired)
    summary["paired_passed"] = {
        arm: sum(
            r["reward"] > 0 for cohort in paired for r in cohort if r["arm"] == arm
        )
        for arm in ARMS
    }
    return rows, next_work, blocked, summary


def pilot_gate(root, pilot):
    rows, pending, blocked, summary = status(root, pilot)
    if pending or blocked:
        return {
            "passed": False,
            "status": "running" if pending else "blocked",
            "summary": summary,
        }
    used = {arm: [] for arm in ARMS[1:]}
    errors = []
    for row in rows:
        if row["arm"] == "baseline" or row["status"] != "complete":
            continue
        trial = Path(row["result"]).parent
        meta = read_json(trial / "agent/rsi_end.json", {})
        if meta.get("controller_runtime_error"):
            errors.append(
                {"task": row["task"], "error": meta["controller_runtime_error"]}
            )
        if (meta.get("version") or {}).get("revision", 0) > 0:
            if row["arm"] == "skill-rsi" and meta.get("selected_skills"):
                used[row["arm"]].append(row["task"])
            elif row["arm"] == "harness-rsi":
                event_path = trial / "agent/rsi_events.jsonl"
                events = [
                    json.loads(line) for line in event_path.read_text().splitlines()
                ]
                if any(
                    e.get("event") == "controller_hook"
                    and e.get("changed")
                    and e.get("hook") != "before_model"
                    for e in events
                ):
                    used[row["arm"]].append(row["task"])
    passed = all(used.values()) and not errors
    return {
        "passed": passed,
        "status": "passed" if passed else "needs_review",
        "used_modified_versions": used,
        "runtime_errors": errors,
        "summary": summary,
    }


def source_hashes():
    paths = [
        PROJECT / "rsi_harness" / name
        for name in (
            "clean_rsi.py",
            "clean_terminal_agent.py",
            "clean_campaign.py",
            "program.py",
        )
    ]
    return {str(p.relative_to(PROJECT)): digest(p.read_text()) for p in paths}


def supervise(args):
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "supervisor.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = {
        "schema": SCHEMA,
        "model": MODEL,
        "arms": ARMS,
        "solve": SOLVE,
        "improve": IMPROVE,
        "pilot": args.pilot,
        "old_root": str(args.old_root),
        "code": source_hashes(),
        "baseline_policy": "reuse-compatible-exclude-incompatible-no-repeat",
        "chain_reset": True,
        "selection": "protocol-only-no-reward-replay",
        "ports": PORTS,
    }
    existing = read_json(root / "manifest.json")
    if existing and digest(existing) != digest(manifest):
        raise RuntimeError(
            "campaign configuration/code changed; use a new versioned root"
        )
    atomic_json(root / "manifest.json", manifest)
    active = read_json(root / "active.json", {})
    stopping = False

    def stop(_s, _f):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    all_chains = chains()
    if any(chain not in all_chains for chain in args.pilot):
        raise ValueError("unknown pilot chain")
    while not stopping:
        if (root / "PAUSE").exists():
            break
        for port, worker in list(active.items()):
            if process_identity(worker["pid"]) != worker["identity"]:
                # A failed worker without a terminal record needs review, never blind retry.
                record = (
                    stage_path(root, worker["chain"], worker["arm"], worker["task"])
                    / "record.json"
                )
                if not record.exists():
                    child = read_json(record.parent / "child.json", {})
                    if child and process_identity(child["pid"]) == child["identity"]:
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
        gate = pilot_gate(root, args.pilot)
        selected = all_chains if gate["passed"] else args.pilot
        rows, pending, blocked, summary = status(root, selected)
        occupied = {int(w[0]) for w in live_chain_workers(args.old_root)}
        active_keys = {(w["chain"], w["arm"]) for w in active.values()}
        count = {
            (c, a): sum(
                r["chain"] == c
                and r["arm"] == a
                and r["status"] in ("complete", "excluded")
                for r in rows
            )
            for c, a, _ in pending
        }
        started_chains = {r["chain"] for r in rows} | {
            w["chain"] for w in active.values()
        }
        pending.sort(
            key=lambda w: (
                w[0] not in started_chains,
                all_chains.index(w[0]),
                count[w[:2]],
                ARMS.index(w[1]),
            )
        )
        waiting = []
        chain_readiness = {}
        disk = docker_disk_status(PROJECT)
        endpoints = {str(port): endpoint_ok(port) for port in PORTS}
        for port in PORTS:
            if (
                not disk["ok"]
                or str(port) in active
                or port in occupied
                or not endpoints[str(port)]
            ):
                continue
            chosen = None
            for candidate in pending:
                chain, arm, task = candidate
                if (chain, arm) in active_keys:
                    continue
                if chain not in started_chains:
                    if chain not in chain_readiness:
                        chain_readiness[chain] = next(
                            (
                                (name, why)
                                for name in tasks(chain)
                                for ok, why in [ready(name, args.old_root)]
                                if not ok
                            ),
                            None,
                        )
                    if chain_readiness[chain]:
                        name, why = chain_readiness[chain]
                        waiting.append(
                            {"chain": chain, "arm": arm, "task": name, "reason": why}
                        )
                        continue
                ok, why = ready(task, args.old_root)
                if not ok:
                    waiting.append(
                        {"chain": chain, "arm": arm, "task": task, "reason": why}
                    )
                    continue
                chosen = candidate
                break
            if chosen:
                chain, arm, task = chosen
                log_path = root / "logs" / f"{chain}-{arm}-{task}.log"
                log_path.parent.mkdir(exist_ok=True)
                command = [
                    sys.executable,
                    "-u",
                    "-m",
                    "rsi_harness.clean_campaign",
                    "stage",
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
                ]
                with log_path.open("ab", buffering=0) as stream:
                    process = subprocess.Popen(
                        command,
                        cwd=PROJECT,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                active[str(port)] = {
                    "pid": process.pid,
                    "identity": process_identity(process.pid),
                    "chain": chain,
                    "arm": arm,
                    "task": task,
                    "started": time.time(),
                }
                active_keys.add((chain, arm))
                pending.remove(chosen)
                atomic_json(root / "active.json", active)
        atomic_json(root / "active.json", active)
        atomic_json(
            root / "status.json",
            {
                "time": time.time(),
                "supervisor_pid": os.getpid(),
                "phase": "full" if gate["passed"] else "pilot",
                "pilot_gate": gate,
                "summary": summary,
                "active": active,
                "waiting": waiting,
                "blocked": blocked,
                "disk": disk,
                "endpoints": endpoints,
                "old_workers_draining_ports": sorted(occupied),
                "terminal": (
                    None
                    if pending or active
                    else "complete"
                    if gate["passed"] and not blocked
                    else "needs_review"
                ),
            },
        )
        if not pending and not active:
            return 0
        time.sleep(20)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("stage", "supervise"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--old-root", type=Path, default=OLD_ROOT)
    parser.add_argument(
        "--pilot", nargs="+", default=["break-filter-js-from-html", "chess-best-move"]
    )
    parser.add_argument("--chain")
    parser.add_argument("--task")
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--port", type=int, choices=PORTS)
    args = parser.parse_args()
    args.root = args.root.resolve()
    if args.operation == "stage" and not all(
        (args.chain, args.task, args.arm, args.port)
    ):
        parser.error("stage needs chain, task, arm, port")
    return run_stage(args) if args.operation == "stage" else supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
