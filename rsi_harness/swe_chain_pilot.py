"""One native SWE-Chain attrs chain, paired across three clean RSI arms.

Generation uses upstream ChainTask and hidden checks. Final metrics use the
upstream replay/compute pipeline. Source and oracle are frozen by the manifest.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

from . import clean_rsi
from . import deepseek_pilot as gateway
from .deepseek_batch import peak_estimate
from .swe_agent import SWEAgent
from .swe_rootless import patch_upstream

PROJECT = Path(__file__).resolve().parents[1]
SWE = PROJECT / "benchmarks/vendor/swe-chain"
DATA = SWE / "data/attrs_21.3.0_to_26.1.0_specs_chain.jsonl"
ROOT = PROJECT / "runs/swe-chain-deepseek-flash-attrs-v2-20260924"
PORT = 8057
MODEL = "deepseek-flash"
ARMS = clean_rsi.ARMS
MAX_PEAK_ESTIMATE_RMB = 40.0


def vendor():
    if str(SWE) not in sys.path:
        sys.path.insert(0, str(SWE))
    patch_upstream()
    from generate.chain import AGENT_CLASSES, ChainRunner
    return AGENT_CLASSES, ChainRunner


def rows() -> list[dict]:
    return [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]


def versions() -> list[str]:
    data = rows()
    return [data[0]["prev_ver"], *(row["next_ver"] for row in data)]


def manifest() -> dict:
    pilot = clean_rsi.read_json(gateway.ROOT / "manifest.json")
    if not pilot or pilot["model"] != MODEL:
        raise RuntimeError("DeepSeek pilot protocol unavailable")
    source = {
        str(path.relative_to(PROJECT)): clean_rsi.digest(path.read_text())
        for path in (
            PROJECT / "rsi_harness/clean_rsi.py",
            PROJECT / "rsi_harness/swe_agent.py",
            PROJECT / "rsi_harness/swe_rootless.py",
            Path(__file__),
        )
    }
    data = rows()
    if len(data) != 13 or data[0]["prev_ver"] != "21.3.0":
        raise RuntimeError("unexpected attrs chain data")
    return {
        "schema": "swe-chain-clean-rsi-attrs-pilot-v2",
        "benchmark": "SWE-Chain",
        "model": MODEL,
        "arms": ARMS,
        "chain": "attrs_21.3.0_to_26.1.0",
        "steps": [(x["prev_ver"], x["next_ver"]) for x in data],
        "solve": clean_rsi.SOLVE,
        "improve": clean_rsi.IMPROVE,
        "chain_reset": True,
        "task_container": "fresh-network-none-singleuid-root-no-released-history",
        "upstream_difference": "host-side agent; root inside single-UID isolated task container",
        "score": "native_replay_curr_prev_F1",
        "json_response_policy": "one_corrective_retry_then_block_on_invalid_response",
        "data_sha256": clean_rsi.digest(DATA.read_text()),
        "sources": source,
        "pilot_manifest_sha256": clean_rsi.digest(pilot),
        "peak_estimate_stop_rmb": MAX_PEAK_ESTIMATE_RMB,
    }


def preflight() -> list[str]:
    failures = []
    data = rows()
    for version in versions():
        if not (SWE / ".packages/attrs" / version).is_dir():
            failures.append("source:" + version)
        image = subprocess.run(
            ["docker", "image", "inspect", f"swe-chain:attrs-{version}"],
            capture_output=True, check=False,
        )
        if image.returncode:
            failures.append("image:" + version)
    oracle = SWE / "oracle/attrs_21.3.0_to_26.1.0"
    for row in data:
        target = row["next_ver"]
        previous = row["prev_ver"]
        for name in (f"v{target}_test_results.json", f"v{previous}_cross_test_results.json"):
            if not (oracle / target / name).is_file():
                failures.append("oracle:" + target + "/" + name)
    return failures


def step_record(root: Path, arm: str, next_version: str) -> Path:
    return root / arm / "stages" / next_version / "record.json"


def progress(root: Path) -> dict:
    data = rows()
    counts = {}
    blocked = []
    for arm in ARMS:
        count = 0
        for row in data:
            record = clean_rsi.read_json(step_record(root, arm, row["next_ver"]))
            if record is None:
                break
            if record.get("status") != "complete":
                blocked.append({"arm": arm, "step": row["next_ver"], "record": record})
                break
            count += 1
        counts[arm] = count
    return {
        "time": time.time(),
        "steps_per_arm": len(data),
        "completed": counts,
        "paired_steps": min(counts.values()),
        "blocked": blocked,
        "usage": peak_estimate(root),
    }


def evaluate_campaign(root: Path) -> dict:
    """Replay every finished arm and require a complete native score table."""
    vendor()
    from evaluation.chain_compute import ChainCompute
    from evaluation.chain_replay import ChainReplay
    from evaluation.utils import iter_phases, read_jsonl

    summary = {}
    for arm in ARMS:
        state = root / arm
        chain_path = state / "chain.json"
        chain = clean_rsi.read_json(chain_path, {}).get("chain", [])
        if len(chain) != len(rows()) or any(not step.get("success") for step in chain):
            raise RuntimeError(f"{arm}: generation is not complete")
        replay = ChainReplay(str(chain_path), mode="both", max_workers=1, resume=True)
        replay.run()
        curr_path = state / "replay_curr_results.jsonl"
        prev_path = state / "replay_prev_results.jsonl"
        curr = list(read_jsonl(curr_path))
        prev = list(read_jsonl(prev_path))
        expected_curr = sum(len(iter_phases(step)) for step in chain)
        expected_prev = len(chain) - 1
        if len(curr) != expected_curr or len(prev) != expected_prev:
            raise RuntimeError(
                f"{arm}: incomplete replay, curr={len(curr)}/{expected_curr}, "
                f"prev={len(prev)}/{expected_prev}"
            )
        ChainCompute(str(chain_path), str(curr_path), str(prev_path)).run()
        evaluation = clean_rsi.read_json(state / "eval.json", {})
        summary[arm] = evaluation.get("overall")
        if not summary[arm]:
            raise RuntimeError(f"{arm}: native score missing")
        clean_rsi.atomic_json(root / "evaluation_status.json", {
            "time": time.time(), "completed": summary,
        })
    return summary


def authenticated_improve(store, arm, task, instruction, trial, reward,
                          token: str):
    original = urllib.request.Request

    def request(url, *a, **kw):
        if str(url).startswith(f"http://127.0.0.1:{PORT}/v1/chat/completions"):
            headers = dict(kw.get("headers") or {})
            headers["Authorization"] = "Bearer " + token
            kw["headers"] = headers
        return original(url, *a, **kw)

    with patch.object(urllib.request, "Request", request):
        return clean_rsi.improve(
            store, arm, task, instruction, trial, reward,
            f"http://127.0.0.1:{PORT}/v1", MODEL,
        )


def run_one_step(root: Path, arm: str, token: str) -> int:
    classes, ChainRunner = vendor()
    data = rows()
    state = root / arm
    store = state / "state"

    def make_agent(model, timeout, effort):
        return SWEAgent(model, arm, store if arm != "baseline" else None,
                        f"http://127.0.0.1:{PORT}/v1", token, timeout)

    agent_name = "rsi-" + arm
    classes[agent_name] = make_agent

    class PilotRunner(ChainRunner):
        def prepare_environment(self):
            missing = preflight()
            if missing:
                raise RuntimeError("SWE assets unavailable: " + ", ".join(missing[:5]))

    cwd = Path.cwd()
    os.chdir(SWE)
    try:
        runner = PilotRunner(
            data_file=str(DATA), model=MODEL, provider="deepseek", agent=agent_name,
            each_timeout=clean_rsi.SOLVE["timeout"], max_iters=2,
        )
        state.mkdir(parents=True, exist_ok=True)
        runner.output_dir = state
        runner.output_path = state / "chain.json"
        runner.record_path = state / "record.jsonl"
        runner.live_log_path = state / "live_log.jsonl"
        runner.build_tasks()
        previous = clean_rsi.read_json(runner.output_path, {}).get("chain", [])
        if any(not item.get("success") for item in previous):
            raise RuntimeError("prior unsuccessful step requires review")
        index = len(previous)
        if index >= len(data):
            return 0
        if any(not step_record(root, arm, x["next_ver"]).exists() for x in data[:index]):
            raise RuntimeError("prior step record missing; no blind continuation")
        if previous:
            runner.chain = previous
            runner.replay_diffs()
        # Root is required by this single-UID daemon. Never expose /released
        # historical snapshots to the solver; only current code/spec remain.
        runner.container.exec("rm -rf /released && mkdir -m 700 /released")
        task = runner.tasks[index]
        stage = state / "stages" / task.next_version
        if (stage / "launch.json").exists():
            raise RuntimeError("step already launched; no duplicate solve")
        clean_rsi.atomic_json(stage / "launch.json", {
            "time": time.time(), "arm": arm, "prev": task.prev_version,
            "next": task.next_version, "container": runner.container.name,
        })
        runner.agent.begin_task(task.next_version, task.specs_str)
        try:
            result = task.run()
        finally:
            runner.agent.close_task()
        trajectory = result.get("trajectory") or []
        trial = stage / "trial"
        clean_rsi.atomic_json(trial / "agent/trajectory.json", {"steps": trajectory})
        runner.save_step(result)
        if not result["success"]:
            clean_rsi.atomic_json(stage / "record.json", {
                "status": "blocked", "reason": result.get("error") or "native_step_failed",
            })
            return 2
        improvement = None
        if arm != "baseline" and index < len(data) - 1:
            improvement = authenticated_improve(
                store, arm, task.next_version, task.specs_str, trial,
                result.get("pass_rate") or 0.0, token,
            )
        clean_rsi.atomic_json(stage / "record.json", {
            "status": "complete", "arm": arm, "prev": task.prev_version,
            "next": task.next_version, "pass_rate": result.get("pass_rate"),
            "improvement": improvement, "finished": time.time(),
        })
        runner.container.stop()
        return 0
    finally:
        os.chdir(cwd)


def run_campaign(root: Path, key: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    candidate = manifest()
    existing = clean_rsi.read_json(root / "manifest.json")
    if existing and clean_rsi.digest(existing) != clean_rsi.digest(candidate):
        raise RuntimeError("campaign manifest changed")
    clean_rsi.atomic_json(root / "manifest.json", candidate)
    token = gateway.secrets.token_urlsafe(32)
    clean_rsi.atomic_json(root / "gateway.json", {"token": token})
    server = gateway.Gateway(("127.0.0.1", PORT), key, token, root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    active = {}
    children = {}
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            for arm, process in list(children.items()):
                if process.poll() is None:
                    continue
                target = active[arm]["step"]
                record = step_record(root, arm, target)
                if not record.exists():
                    clean_rsi.atomic_json(record, {
                        "status": "blocked", "reason": "worker_exited_without_record",
                    })
                print(json.dumps({"event": "step_finished", "arm": arm,
                                  "step": target, "exit_code": process.returncode}), flush=True)
                children.pop(arm)
                active.pop(arm)
            info = progress(root)
            budget_hit = info["usage"]["peak_estimate_rmb"] >= MAX_PEAK_ESTIMATE_RMB
            paused = stopping or (root / "PAUSE").exists() or budget_hit
            if not paused and not info["blocked"]:
                for arm in ARMS:
                    if arm in active or info["completed"][arm] >= len(rows()):
                        continue
                    step = rows()[info["completed"][arm]]["next_ver"]
                    log = root / arm / "stages" / step / "worker.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    command = [sys.executable, "-u", "-m", "rsi_harness.swe_chain_pilot",
                               "stage", "--arm", arm]
                    with log.open("ab", buffering=0) as stream:
                        process = subprocess.Popen(
                            command, cwd=PROJECT, env=os.environ.copy(),
                            stdout=stream, stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    active[arm] = {"step": step, "pid": process.pid,
                                   "started": time.time()}
                    children[arm] = process
                    print(json.dumps({"event": "step_started", "arm": arm,
                                      "step": step, "pid": process.pid}), flush=True)
            terminal = None
            if not active:
                if info["blocked"]:
                    terminal = "needs_review"
                elif info["paired_steps"] == len(rows()):
                    terminal = "generation_complete"
                elif paused:
                    terminal = "budget_pause" if budget_hit else "paused"
            clean_rsi.atomic_json(root / "status.json", {
                **progress(root), "active": active, "terminal": terminal,
            })
            if terminal:
                print(json.dumps({"event": terminal, "time": time.time()}), flush=True)
                break
            time.sleep(5)
    finally:
        server.shutdown()
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            (root / "gateway.json").unlink()
    if clean_rsi.read_json(root / "status.json", {}).get("terminal") != "generation_complete":
        return 2
    try:
        scores = evaluate_campaign(root)
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        clean_rsi.atomic_json(root / "status.json", {
            **progress(root), "active": {}, "terminal": "evaluation_needs_review",
            "evaluation_error": f"{type(exc).__name__}: {exc}",
        })
        raise
    clean_rsi.atomic_json(root / "status.json", {
        **progress(root), "active": {}, "terminal": "evaluation_complete",
        "native_scores": scores,
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start", "stage", "evaluate"))
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    if args.operation == "stage":
        if args.arm is None:
            raise ValueError("arm required")
        expected = manifest()
        if clean_rsi.digest(clean_rsi.read_json(ROOT / "manifest.json")) != clean_rsi.digest(expected):
            raise RuntimeError("frozen SWE protocol changed")
        token = clean_rsi.read_json(ROOT / "gateway.json", {}).get("token")
        if not token:
            raise RuntimeError("gateway unavailable")
        return run_one_step(ROOT, args.arm, token)
    if args.operation == "evaluate":
        if clean_rsi.digest(clean_rsi.read_json(ROOT / "manifest.json")) != clean_rsi.digest(manifest()):
            raise RuntimeError("frozen SWE protocol changed")
        scores = evaluate_campaign(ROOT)
        clean_rsi.atomic_json(ROOT / "status.json", {
            **progress(ROOT), "active": {}, "terminal": "evaluation_complete",
            "native_scores": scores,
        })
        return 0
    if preflight():
        print(json.dumps({"event": "preflight_failed", "reasons": preflight()}))
        return 2
    key = getpass.getpass("DeepSeek API key (hidden): ").strip()
    if not key:
        raise ValueError("API key required")
    pid = os.fork()
    if pid:
        print(json.dumps({"event": "swe_daemon_started", "pid": pid, "root": str(ROOT)}))
        return 0
    os.setsid()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    log = (ROOT / "campaign.log").open("a", buffering=1)
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
