"""Four independent formal arms on one complete official chain."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import os
from pathlib import Path
import secrets
import threading
import time
import traceback

from swe_chain_evo.common import ROOT, PROJECT, atomic, read, sha
from swe_chain_evo.campaign import source_pins as core_pins
from .update_worker import update_subprocess
from .trial import TrialRunner
from methods.evo_fifty.gateway import Server
from methods.evo_fifty.native_meta import VENDOR
from methods.evo_fifty.spend import SpendBlocked
from methods.evo_fifty.trial import NetworkUnavailable
from .budget import Broker
from .report import write_report, plots
from .acceptance import first_task, first_update

ARMS = ("kira-static", "meta-harness")


def source_pins(root):
    paths = list((ROOT / "formal_swe_chain").glob("*.py"))
    from swe_chain_evo.offline_go_dependencies import TOOL
    paths += [TOOL]
    paths += [p for p in (root / "initial").rglob("*") if p.is_file()]
    paths += [root / name for name in ("initial-state.json", "launch-gates.json", "harbor-preflight.json",
                                      "image-provenance.json", "protocol-frozen.json", "formal-score-preflight.json")]
    paths += [root / "inheritance-provenance.json"]
    paths += list((root / "preparation/tasks").glob("*.json"))
    paths += [ROOT / "runs/grader-preflight" / e["instance_id"] / "test-id-map.json"
              for e in read(root / "task-stream.json")]
    return {**core_pins(root), **{str(p): sha(p) for p in paths}}


def should_update(config, arm, completed, total):
    every = config["arms"][arm].get("update_every_completed_tasks", 0)
    return bool(every and completed % every == 0 and completed < total)


def validate(root, config):
    stream = read(root / "task-stream.json")
    if (config["benchmark"] != "SWE-Chain-Evo"
            or len(stream) != 15 or len({e["chain_id"] for e in stream}) != 1
            or [e["version_chain_index"] for e in stream] != list(range(15))
            or config.get("chain_id") != stream[0]["chain_id"]):
        raise ValueError("formal entry requires the configured complete ordered chain")
    if "development_limit_usd" in config or "development_forced_update_indices" in config["runtime"]:
        raise ValueError("development overrides present in formal configuration")
    if not read(root / "harbor-preflight.json").get("passed"):
        raise RuntimeError("zero-model Harbor preflight not passed")
    if not read(root / "launch-gates.json").get("passed"):
        raise RuntimeError("RSI development gates not passed")
    if not read(root / "formal-score-preflight.json").get("passed"):
        raise RuntimeError("formal score normalization preflight not passed")
    for i in range(len(stream)):
        check = read(root / "preparation/tasks" / f"{i}.json")
        if (not check.get("passed") or check["grader_sha256"] != sha(ROOT / "swe_chain_evo/grading.py")):
            raise RuntimeError("task grader preflight missing or stale")


def run(root):
    from within_chain.contracts import require_authorization
    require_authorization(root)
    lock = (root / ".campaign.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = read(root / "configuration.json")
    validate(root, config)
    broker = Broker(root, Path(config["credential_file"]).read_text().strip(), config)
    runner = TrialRunner(root, broker, config["runtime"]["gateway_port"])
    pins = source_pins(root)
    pinfile = root / "native-source-pins.json"
    if pinfile.exists() and read(pinfile) != pins:
        raise RuntimeError("source/configuration changed; preserve the previous paid run")
    atomic(pinfile, pins)
    runner.pins = pins
    statefile = root / "native-campaign.json"
    state = read(statefile) if statefile.exists() else {
        "schema": "swe-chain-evo-formal-campaign-v1", "created": time.time(), "development_only": False,
        "configuration_sha256": sha(root / "configuration.json"),
        "arms": {a: {"rows": [], "parent": str(PROJECT / "methods/vendor/meta-harness/reference_examples/terminal_bench_2/agents/baseline_kira.py"),
            "candidate": None, "pending_update": None, "status": "ready"} for a in ARMS}}
    if state["configuration_sha256"] != sha(root / "configuration.json"):
        raise RuntimeError("saved campaign configuration changed")
    controls = {a: secrets.token_urlsafe(32) for a in ("meta-harness",)}
    server = Server(("127.0.0.1", runner.port), broker, {t: a for a, t in controls.items()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["active"] = {}
    plotted_prefix = -1

    def save(status="running"):
        nonlocal plotted_prefix
        state.update(state=status, updated=time.time(), pid=os.getpid())
        atomic(statefile, state)
        atomic(root / "status.json", {k: v for k, v in state.items() if k != "arms"})
        report = write_report(root, state, broker)
        prefix = report["matched_completed_tasks"]
        if prefix != plotted_prefix:
            try:
                plots(root, report)
                plotted_prefix = prefix
            except Exception as exc:
                atomic(root / "plot-error.json", {"error": str(exc), "time": time.time()})

    save()
    total = len(runner.stream)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            last_heartbeat = 0
            while True:
                if (config["runtime"].get("first_task_acceptance_gate")
                        and not (root / "first-task-acceptance.json").exists()
                        and all(i["rows"] for i in state["arms"].values())):
                    accepted = first_task(root, state, config)
                    if not accepted["passed"]:
                        (root / "PAUSE").write_text("First-task integration audit requires review.\n")
                paused = (root / "PAUSE").exists()
                pilot_limit = config['runtime'].get('pilot_formal_task_limit')
                if pilot_limit and all(len(i['rows']) >= pilot_limit for i in state['arms'].values()):
                    (root / 'PAUSE').write_text('Configured first-task pilot complete; review before continuation.\n')
                    paused = True
                for arm in ARMS:
                    item = state["arms"][arm]
                    if pilot_limit and len(item['rows']) >= pilot_limit:
                        continue
                    if (config["runtime"].get("first_task_acceptance_gate")
                            and item["rows"]
                            and not (root / "first-task-acceptance.json").exists()):
                        continue
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
                            modules=Path(item["parent"]) if "modular" in arm else None,
                            candidate=item["candidate"])
                    futures[future] = (arm, phase, index)
                if not futures:
                    if not paused and any(i["status"] == "waiting_for_network" for i in state["arms"].values()):
                        time.sleep(1)
                        continue
                    complete = all(len(i["rows"]) == total and i["pending_update"] is None
                                   for i in state["arms"].values())
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
                            if should_update(config, arm, len(item["rows"]), total):
                                item["pending_update"] = index
                        else:
                            if not (root / ("first-update-acceptance-" + arm + ".json")).exists():
                                audit = first_update(root, arm, config)
                                if not audit["passed"]:
                                    raise RuntimeError("first native RSI update integration audit failed")
                            item.setdefault("updates", []).append({"index": index, **result})
                            if result["candidate"]:
                                item["parent"] = result["candidate"]
                                if arm == "meta-harness":
                                    item["candidate"] = result["candidate"]
                            item["pending_update"] = None
                        item["status"] = "ready"
                        item.pop("error", None)
                    except BaseException as exc:
                        item["error"] = {"type": type(exc).__name__, "message": str(exc),
                                         "traceback": traceback.format_exc()}
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "runs/pilot-15-click-20261015-v1")
    run(parser.parse_args().root.resolve())
