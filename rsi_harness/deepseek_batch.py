"""Resumable, Qwen-order ten-chain DeepSeek Flash comparison.

The frozen clean-RSI worker is reused unchanged. The completed one-chain pilot
is linked read-only into this campaign; no pilot trial is solved again.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

from . import clean_campaign as core
from . import deepseek_pilot as pilot
from .clean_rsi import ARMS, IMPROVE, SOLVE, atomic_json, digest, read_json
from .clean_scheduler import paired_metrics
from .terminal_infra import docker_disk_status

MODEL = pilot.MODEL
PORT = 8056
ROOT = core.PROJECT / "runs/deepseek-flash-qwen-order-10chain-20260924"
ENV_FILE_NAME = "llm-proxy.env"
PILOT_ROOT = pilot.ROOT
MAX_PEAK_ESTIMATE_RMB = 40.0
CHAINS = (
    "break-filter-js-from-html",
    "chess-best-move",
    "bn-fit-modify",
    "build-cython-ext",
    "build-pmars",
    "caffe-cifar-10",
    "cancel-async-tasks",
    "circuit-fibsqrt",
    "cobol-modernization",
    "compile-compcert",
)


def manifest() -> dict:
    qwen = read_json(
        core.PROJECT / "runs/clean-rsi-20260923-qwen38-27b/scheduler_manifest.json"
    )
    pilot_manifest = read_json(PILOT_ROOT / "manifest.json")
    if not qwen or not pilot_manifest:
        raise RuntimeError("Qwen order or DeepSeek pilot manifest unavailable")
    if tuple(chain for chain in qwen["chain_order"] if chain in CHAINS) != CHAINS:
        raise RuntimeError("batch order differs from Qwen's frozen order")
    if pilot_manifest["frozen_source_hashes"] != core.source_hashes():
        raise RuntimeError("frozen clean-RSI source differs from the pilot")
    if pilot_manifest["pilot_source_sha256"] != digest(
        Path(pilot.__file__).read_text()
    ):
        raise RuntimeError("pilot gateway source changed")
    if pilot_manifest["model"] != MODEL or pilot_manifest["tasks"] != core.tasks(
        pilot.CHAIN
    ):
        raise RuntimeError("pilot protocol incompatible")
    return {
        "schema": "deepseek-qwen-order-ten-chain-v1",
        "model": MODEL,
        "arms": ARMS,
        "solve": SOLVE,
        "improve": IMPROVE,
        "chain_order": CHAINS,
        "task_order": {chain: core.tasks(chain) for chain in CHAINS},
        "baseline_policy": "fresh-solve-no-qwen-reuse",
        "chain_reset": True,
        "frozen_source_hashes": core.source_hashes(),
        "pilot_manifest_sha256": digest(pilot_manifest),
        "pilot_chain_source": str(PILOT_ROOT / pilot.CHAIN),
        "qwen_scheduler_manifest_sha256": digest(qwen),
        "batch_source_sha256": digest(Path(__file__).read_text()),
        "max_peak_estimate_rmb": MAX_PEAK_ESTIMATE_RMB,
    }


def preflight() -> list[dict]:
    failures = []
    for chain in CHAINS:
        for task in core.tasks(chain):
            ok, reason = core.ready(task, core.OLD_ROOT)
            if not ok:
                failures.append({"chain": chain, "task": task, "reason": reason})
    disk = docker_disk_status(core.PROJECT)
    if not disk["ok"]:
        failures.append({"reason": "disk_gate", "detail": disk})
    pilot_rows, pending, blocked, _ = core.status(PILOT_ROOT, [pilot.CHAIN])
    if pending or blocked or len(pilot_rows) != len(core.tasks(pilot.CHAIN)) * len(ARMS):
        failures.append({"reason": "pilot_incomplete"})
    return failures


def peak_estimate(root: Path) -> dict:
    totals = {"cache_hit": 0, "cache_miss": 0, "output": 0, "requests": 0, "errors": 0}
    path = root / "api_usage.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            totals["requests"] += 1
            totals["errors"] += row.get("status") != 200
            usage = row.get("usage") or {}
            totals["cache_hit"] += usage.get("prompt_cache_hit_tokens") or 0
            totals["cache_miss"] += usage.get("prompt_cache_miss_tokens") or 0
            totals["output"] += usage.get("completion_tokens") or 0
    totals["peak_estimate_rmb"] = round(
        (totals["cache_hit"] * 0.04 + totals["cache_miss"] * 2 + totals["output"] * 8)
        / 1_000_000,
        4,
    )
    return totals


def status(root: Path, active: dict, terminal: str | None = None) -> dict:
    rows, pending, blocked, summary = core.status(root, CHAINS)
    return {
        "time": time.time(),
        "model": MODEL,
        "chain_order": CHAINS,
        "summary": summary,
        "paired": paired_metrics(rows, CHAINS),
        "active": active,
        "pending": pending,
        "blocked": blocked,
        "usage": peak_estimate(root),
        "terminal": terminal,
    }


def patched_stage(args) -> int:
    if args.chain not in CHAINS or args.chain == pilot.CHAIN:
        raise ValueError("stage chain not allowed")
    if args.arm not in ARMS or args.task not in core.tasks(args.chain):
        raise ValueError("stage arm or task not allowed")
    expected = manifest()
    if digest(read_json(args.root / "manifest.json")) != digest(expected):
        raise RuntimeError("batch protocol changed")
    token = read_json(args.root / "gateway.json", {}).get("token")
    if not token:
        raise RuntimeError("gateway token unavailable")
    core.MODEL = MODEL
    original_environment = core.runner_environment
    original_improve = core.improve
    original_request = urllib.request.Request

    def runner_environment(port: int) -> dict:
        env = original_environment(port)
        env["HARBOR_EVOMEM_LLM_ENV"] = str(args.root / ENV_FILE_NAME)
        return env

    def authenticated_request(url, *a, **kw):
        if str(url).startswith(f"http://127.0.0.1:{PORT}/v1/chat/completions"):
            headers = dict(kw.get("headers") or {})
            headers["Authorization"] = "Bearer " + token
            kw["headers"] = headers
        return original_request(url, *a, **kw)

    def improve(*a, **kw):
        with patch.object(urllib.request, "Request", authenticated_request):
            return original_improve(*a, **kw)

    with patch.object(core, "runner_environment", runner_environment), patch.object(
        core, "legacy_baseline", lambda *_: None
    ), patch.object(core, "improve", improve):
        return core.run_stage(args)


def next_chain(pending: list[tuple]) -> str | None:
    for chain in CHAINS:
        if any(item[0] == chain for item in pending):
            return chain
    return None


def run_campaign(root: Path, key: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    candidate = manifest()
    existing = read_json(root / "manifest.json")
    if existing and digest(existing) != digest(candidate):
        raise RuntimeError("batch manifest changed; use a new root")
    atomic_json(root / "manifest.json", candidate)
    pilot_link = root / pilot.CHAIN
    expected_target = PILOT_ROOT / pilot.CHAIN
    if pilot_link.is_symlink():
        if pilot_link.resolve() != expected_target.resolve():
            raise RuntimeError("pilot link target changed")
    elif pilot_link.exists():
        raise RuntimeError("pilot chain path exists but is not the frozen link")
    else:
        pilot_link.symlink_to(expected_target, target_is_directory=True)

    token = secrets.token_urlsafe(32)
    atomic_json(root / "gateway.json", {"token": token})
    env_file = root / ENV_FILE_NAME
    env_file.write_text(
        f"LLM_API_KEY={token}\nLLM_MODEL=openai/{MODEL}\n"
        f"LLM_BASE_URL=http://127.0.0.1:{PORT}/v1\n"
    )
    env_file.chmod(0o600)
    server = pilot.Gateway(("127.0.0.1", PORT), key, token, root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(json.dumps({"event": "gateway_ready", "port": PORT}), flush=True)
    active: dict[str, dict] = {}
    children: dict[str, subprocess.Popen] = {}
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            for arm, worker in list(active.items()):
                process = children[arm]
                if process.poll() is None:
                    continue
                record = core.stage_path(root, worker["chain"], arm, worker["task"]) / "record.json"
                if not record.exists():
                    atomic_json(record, {"status": "blocked", "reason": "worker_exited_without_record"})
                print(json.dumps({"event": "stage_finished", "arm": arm,
                                  "chain": worker["chain"], "task": worker["task"],
                                  "exit_code": process.returncode}), flush=True)
                active.pop(arm)
                children.pop(arm)

            _rows, pending, blocked, summary = core.status(root, CHAINS)
            usage = peak_estimate(root)
            budget_hit = usage["peak_estimate_rmb"] >= MAX_PEAK_ESTIMATE_RMB
            paused = stopping or (root / "PAUSE").exists() or budget_hit
            chain = next_chain(pending)
            if not blocked and not paused and chain:
                for candidate_chain, arm, task in pending:
                    if candidate_chain != chain or arm in active:
                        continue
                    log = core.stage_path(root, chain, arm, task) / "stage.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    command = [sys.executable, "-u", "-m", "rsi_harness.deepseek_batch",
                               "stage", "--root", str(root), "--chain", chain,
                               "--arm", arm, "--task", task, "--port", str(PORT)]
                    with log.open("ab", buffering=0) as stream:
                        process = subprocess.Popen(
                            command, cwd=core.PROJECT, env=os.environ.copy(),
                            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                        )
                    active[arm] = {"pid": process.pid, "chain": chain,
                                   "task": task, "started": time.time()}
                    children[arm] = process
                    print(json.dumps({"event": "stage_started", "arm": arm,
                                      "chain": chain, "task": task,
                                      "pid": process.pid}), flush=True)
            terminal = None
            if not active:
                if blocked:
                    terminal = "needs_review"
                elif not pending:
                    terminal = "complete"
                elif paused:
                    terminal = "budget_pause" if budget_hit else "paused"
            atomic_json(root / "status.json", status(root, active, terminal))
            if terminal:
                print(json.dumps({"event": terminal, "time": time.time(),
                                  "summary": summary, "usage": usage}), flush=True)
                break
            time.sleep(5)
    finally:
        server.shutdown()
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            env_file.unlink()
        with contextlib.suppress(FileNotFoundError):
            (root / "gateway.json").unlink()
    return 0 if read_json(root / "status.json", {}).get("terminal") == "complete" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start", "stage"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--chain")
    parser.add_argument("--arm")
    parser.add_argument("--task")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.old_root = core.OLD_ROOT
    if args.operation == "stage":
        return patched_stage(args)
    if args.root != ROOT or args.port != PORT:
        raise ValueError("fixed batch configuration")
    failures = preflight()
    if failures:
        print(json.dumps({"event": "preflight_failed", "failures": failures}))
        return 2
    key = getpass.getpass("DeepSeek API key (hidden): ").strip()
    if not key:
        raise ValueError("API key required")
    pid = os.fork()
    if pid:
        print(json.dumps({"event": "batch_daemon_started", "pid": pid,
                          "root": str(ROOT)}))
        return 0
    os.setsid()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    log = (ROOT / "batch.log").open("a", buffering=1)
    with open(os.devnull, "r") as null:
        os.dup2(null.fileno(), sys.stdin.fileno())
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    try:
        code = run_campaign(ROOT, key)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"event": "coordinator_error", "type": type(exc).__name__,
                          "message": str(exc), "time": time.time()}), flush=True)
        code = 2
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
