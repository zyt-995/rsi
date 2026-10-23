"""Independent parallel arms on one ordered development chain."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
import traceback

from .common import ROOT, PROJECT, PYTHON, read, atomic, sha, existing_imports
existing_imports()
from methods.evo_fifty.gateway import Server
from methods.evo_fifty.native_meta import VENDOR
from methods.evo_fifty.spend import SpendBlocked
from methods.evo_fifty.trial import NetworkUnavailable
from methods.evo_fifty.campaign import source_pins as inherited_pins
from .budget import Broker
from .trial import TrialRunner

ARMS = ("meta-harness",)


def report(root, state, broker):
    receipts = [read(p) for p in (root / "provider-calls").glob("*/request.json")]
    results = []
    for arm in ARMS:
        rows = state["arms"][arm]["rows"]
        calls = [r for r in receipts if r["arm"] == arm and r["state"] == "settled"]
        task = [r for r in calls if r["phase"] == "formal"]
        rsi = [r for r in calls if r["phase"] != "formal"]
        def usage(calls):
            return {"input": sum(r["usage"]["prompt_tokens"] for r in calls),
                    "output": sum(r["usage"]["completion_tokens"] for r in calls)}
        unknown = [r["id"] for r in receipts if r["arm"] == arm and r["state"] == "requires_review"]
        pending = [r["id"] for r in receipts if r["arm"] == arm and r["state"] == "started"]
        results.append({"method": arm, "completed": len(rows), "passed": sum(r["reward"] for r in rows),
                        "step_accuracy": sum(r["reward"] for r in rows) / len(rows) if rows else None,
                        "chain_accuracy": None, "task_tokens": usage(task), "rsi_tokens": usage(rsi),
                        "total_tokens": usage(calls),
                        "cost_usd": sum(r["usage"]["cost"] for r in calls),
                        "usage_complete": not unknown and not pending, "unknown_usage_calls": unknown,
                        "pending_calls": pending,
                        "status": state["arms"][arm].get("status", "ready")})
    atomic(root / "metrics.json", {"development_only": True, "updated": time.time(), "arms": results,
                                   "spend": broker.ledger.snapshot()})


def update_subprocess(runner, arm, index, control_token):
    output = runner.root / "arms" / arm / "steps" / f"{index:06d}" / "update"
    output.mkdir(parents=True, exist_ok=True)
    outcome = output / "worker-outcome.json"
    if outcome.exists():
        data = read(outcome)
        if data["ok"]:
            return data["decision"]
        # Native operators have their own checkpoints; never silently replay
        # an update whose paid continuation has not been reviewed.
        raise RuntimeError("failed update checkpoint retained; inspect " + str(outcome))
    env = dict(runner.env)
    for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        env.pop(name, None)
    env.update(EVO_ONLINE_CONTROL_TOKEN=control_token,
               PYTHONPATH=os.pathsep.join([str(ROOT), str(PROJECT)]),
               TMPDIR=str(runner.root / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    cmd = [str(PYTHON), "-u", "-m", "swe_chain_evo.update_worker", "--root", str(runner.root),
           "--arm", arm, "--index", str(index)]
    with (output / "coordinator-update-worker.log").open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT)
        atomic(output / "worker-launch.json", {"pid": proc.pid, "time": time.time(), "arm": arm, "index": index})
        proc.wait()
    if not outcome.exists():
        raise RuntimeError("native update exited without checkpoint; inspect " + str(output))
    data = read(outcome)
    if not data["ok"]:
        error = data["error"]
        if error["type"] == "SpendBlocked" or "global dollar limit" in error["message"]:
            raise SpendBlocked(error["message"])
        if error["type"] == "NetworkUnavailable":
            raise NetworkUnavailable(error["message"])
        raise RuntimeError(error["type"] + ": " + error["message"])
    return data["decision"]


def source_pins(root):
    pins = inherited_pins(root)
    paths = list((ROOT / "swe_chain_evo").glob("*.py")) + list((ROOT / "swe_chain_evo").glob("*.yaml"))
    paths += list((root / "entries").glob("*.json"))
    from rsi_portable.paths import REPO
    paths += [REPO / "configs/benchmark/seven-chains.json", REPO / "upstream/manifest.json",
              REPO / "upstream/patches/harbor-runtime.patch"]
    paths += list((REPO / "src/runner/rsi_portable").glob("*.py"))
    import harbor
    paths += list(Path(harbor.__file__).parent.rglob("*.py"))
    return {**pins, **{str(p): sha(p) for p in paths}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    lock = (root / ".campaign.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = read(root / "configuration.json")
    if config["benchmark"] != "SWE-Chain-Evo" or config["reporting"]["cohort"] != "development":
        raise ValueError("this entry point is for development integration only")
    preflight = read(root / "harbor-preflight.json")
    if not preflight.get("passed"):
        raise RuntimeError("Harbor zero-call preflight not passed")
    broker = Broker(root, Path(config["credential_file"]).read_text().strip(), config)
    runner = TrialRunner(root, broker, config["runtime"]["gateway_port"])
    pins = source_pins(root)
    pinfile = root / "native-source-pins.json"
    if pinfile.exists() and read(pinfile) != pins:
        raise RuntimeError("source changed since launch; preserve the previous paid run")
    atomic(pinfile, pins)
    runner.pins = pins
    initial = root / "initial/modular/gen_0/modules"
    statefile = root / "native-campaign.json"
    state = read(statefile) if statefile.exists() else {
        "schema": "swe-chain-evo-development-campaign-v1", "created": time.time(),
        "development_only": True, "configuration_sha256": sha(root / "configuration.json"),
        "arms": {a: {"rows": [], "parent": str(initial if "modular" in a else VENDOR / "agents/baseline_kira.py"),
                      "candidate": None, "pending_update": None, "status": "ready"} for a in ARMS}}
    controls = {a: secrets.token_urlsafe(32) for a in ("meta-harness", "modularrsi")}
    server = Server(("127.0.0.1", runner.port), broker, {t: a for a, t in controls.items()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["active"] = {}
    def save(status="running"):
        state.update(state=status, updated=time.time(), pid=os.getpid(), spend=broker.ledger.snapshot())
        atomic(statefile, state)
        atomic(root / "status.json", {k: v for k, v in state.items() if k != "arms"})
        report(root, state, broker)
    save()
    total = len(runner.stream)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            last_heartbeat = 0
            while True:
                paused = (root / "PAUSE").exists()
                for arm in ARMS:
                    item = state["arms"][arm]
                    if paused or arm in state["active"] or item["status"] not in ("ready", "waiting_for_network"):
                        continue
                    if item.get("retry_not_before", 0) > time.time():
                        continue
                    if item["pending_update"] is not None:
                        phase, index = "rsi_update", item["pending_update"]
                    elif len(item["rows"]) < total:
                        phase, index = "formal", len(item["rows"])
                    else:
                        continue
                    state["active"][arm] = {"phase": phase, "index": index, "started": time.time()}
                    save()
                    if phase == "rsi_update":
                        future = pool.submit(update_subprocess, runner, arm, index, controls[arm])
                    else:
                        future = pool.submit(runner.run, arm, index,
                            root / "arms" / arm / "steps" / f"{index:06d}" / "formal",
                            modules=Path(item["parent"]) if "modular" in arm else None, candidate=item["candidate"])
                    futures[future] = (arm, phase, index)
                if not futures:
                    if not paused and any(i["status"] == "waiting_for_network" for i in state["arms"].values()):
                        time.sleep(1)
                        continue
                    complete = all(len(i["rows"]) == total and i["pending_update"] is None for i in state["arms"].values())
                    save("paused" if paused else "completed" if complete else "needs_review")
                    break
                done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    arm, phase, index = futures.pop(future)
                    state["active"].pop(arm)
                    item = state["arms"][arm]
                    try:
                        result = future.result()
                        if phase == "formal":
                            item["rows"].append(result)
                            every = config["arms"][arm].get("update_every_completed_tasks", 0)
                            forced = index in config["runtime"].get("development_forced_update_indices", [])
                            if every and (len(item["rows"]) % every == 0 or forced):
                                # Last-task update is only an explicit development
                                # exercise, never counted as a formal experiment.
                                if len(item["rows"]) < total or config["runtime"]["development_update_at_end"]:
                                    item["pending_update"] = index
                        else:
                            item.setdefault("updates", []).append({"index": index, **result})
                            if result["candidate"]:
                                item["parent"] = result["candidate"]
                                if arm == "meta-harness":
                                    item["candidate"] = result["candidate"]
                            item["pending_update"] = None
                        item["status"] = "ready"
                        item.pop("error", None)
                    except BaseException as exc:
                        item["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
                        item["status"] = "budget_exhausted" if isinstance(exc, SpendBlocked) else "needs_review"
                        if isinstance(exc, NetworkUnavailable):
                            item.update(status="waiting_for_network", retry_not_before=time.time() + 60)
                    save()
                if time.time() - last_heartbeat >= 20:
                    atomic(root / "heartbeat.json", {"time": time.time(), "pid": os.getpid(), "active": state["active"]})
                    save()
                    last_heartbeat = time.time()
    finally:
        server.shutdown()
        server.server_close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
