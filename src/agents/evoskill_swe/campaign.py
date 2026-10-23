"""Run the added EvoSkill arm independently, retaining the existing lifetime cap."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import shutil
import threading
import time
import traceback

from . import RELEASE, VENDOR, SDK_SITE
from .native import update
from .trial import TrialRunner
from .report import report, plots
from swe_chain_evo.common import atomic, read, sha
from formal_swe_chain.budget import Broker
from methods.evo_fifty.gateway import Server
from methods.evo_fifty.spend import SpendBlocked
from methods.evo_fifty.trial import NetworkUnavailable


def pin_sources(root, config):
    source = Path(config["comparison_source"]["run"])
    pins = read(source / "native-source-pins.json")
    paths = [*RELEASE.glob("evoskill_swe/*.py"), *[p for p in RELEASE.glob("scripts/*") if p.is_file()],
             root / "configuration.json", root / "task-stream.json", root / "tokenizer.json"]
    paths += [p for p in VENDOR.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    paths += list((SDK_SITE / "claude_agent_sdk").rglob("*.py"))
    paths += list((root / "entries").glob("*.json"))
    paths += [p for p in (root / "initial").rglob("*") if p.is_file()]
    paths += [root / "inheritance-provenance.json"]
    paths += [Path(shutil.which("claude")).resolve(), root / "source-provenance.json",
              root / "offline-checks.json", root / "skill-mount-preflight.json"]
    return {**pins, **{str(p): sha(p) for p in paths}}


def validate(root, config):
    source = Path(config["comparison_source"]["run"])
    previous = read(source / "configuration.json")
    for field in ("model", "solve", "accounting", "spend", "evaluation_corrections", "chain_protocol"):
        if config[field] != previous[field]:
            raise ValueError("comparability contract differs: " + field)
    if read(root / "task-stream.json") != read(source / "task-stream.json"):
        raise ValueError("task stream differs from other baselines")
    if config["solve"]["max_input_plus_output_tokens"] is not None:
        raise ValueError("unexpected cumulative token cutoff")
    if not read(root / "offline-checks.json").get("passed"):
        raise RuntimeError("offline integration checks not passed")
    if not read(root / "skill-mount-preflight.json").get("passed"):
        raise RuntimeError("offline skill mount/verifier preflight not passed")
    for name in ("first-task-acceptance.json", "first-update-acceptance.json"):
        if (root / name).exists() and not read(root / name).get("passed"):
            from .context_recovery import validate_pending_context_recovery
            # Allow only the audited unfinished update to reach its original
            # acceptance again. Formal task4 remains blocked behind update3.
            if name == "first-update-acceptance.json" and validate_pending_context_recovery(root):
                continue
            raise RuntimeError("recorded execution gate requires review: " + name)


def acceptance(root, runner, row):
    result, grade = read(row["result"]), read(row["grade"])
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    requests = [read(root / "provider-calls" / cid / "request.json") for cid in row["call_ids"]]
    payloads = [r["payload"] for r in requests if "payload" in r]
    # Model/provider contract is recorded in the gateway's request payload files.
    bad = [p for p in payloads if p.get("model") != runner.config["model"]["id"]
           or p.get("reasoning") != runner.config["model"]["reasoning"]
           or p.get("temperature") != runner.config['model'].get('temperature')
           or p.get("top_p") != runner.config['model'].get('top_p')
           or p.get("provider", {}).get("only") != runner.config['model']['provider']['only']
           or not 0 < p.get("max_tokens", 0) <= runner.config['model']['max_output_tokens']]
    passed = (not row.get("exception") and not metadata.get("budget_stop_reason")
              and grade.get("required_tests", 0) > 0 and not bad
              and len(payloads) == len(requests) and bool(requests))
    value = {"passed": passed, "usage_complete": row["usage_complete"],
             "full_accounting_acceptance": row["usage_complete"],
             "normal_wrong_answer_is_allowed": True, "reward": row["reward"],
             "model_contract_mismatches": len(bad), "provider_calls": len(requests),
             "agent_exception": row.get("exception"), "budget_stop_reason": metadata.get("budget_stop_reason"),
             "required_tests": grade.get("required_tests"), "passed_tests": grade.get("passed_required_tests"),
             "normalizer": grade.get("test_id_normalization"), "time": time.time()}
    atomic(root / "first-task-acceptance.json", value)
    if not passed:
        raise RuntimeError("first-task execution acceptance failed")


def run(root):
    root = Path(root)
    lock = (root / ".campaign.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = read(root / "configuration.json")
    validate(root, config)
    broker = Broker(root, Path(config["credential_file"]).read_text().strip(), config)
    runner = TrialRunner(root, broker, config["runtime"]["gateway_port"])
    pins = pin_sources(root, config)
    pinfile = root / "native-source-pins.json"
    if pinfile.exists() and read(pinfile) != pins:
        raise RuntimeError("pinned source/config changed; preserve the paid run")
    atomic(pinfile, pins)
    runner.pins = pins
    statefile = root / "native-campaign.json"
    state = read(statefile) if statefile.exists() else {"schema": "evoskill-swe-chain-campaign-v1",
        "created": time.time(), "configuration_sha256": sha(root / "configuration.json"),
        "arms": {"evoskill": {"rows": [], "updates": [], "skills": str(root / "initial/skills"),
                  "workspace": None, "pending_update": None, "status": "ready"}}, "active": {}}
    if state["configuration_sha256"] != sha(root / "configuration.json"):
        raise ValueError("saved experiment configuration changed")
    server = Server(("127.0.0.1", runner.port), broker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous_plot = -1

    def save(status="running"):
        nonlocal previous_plot
        state.update(state=status, updated=time.time(), pid=os.getpid())
        atomic(statefile, state)
        atomic(root / "status.json", {k: v for k, v in state.items() if k != "arms"})
        value = report(root, state, broker)
        if value["matched_completed_tasks"] != previous_plot:
            plots(root, value)
            previous_plot = value["matched_completed_tasks"]

    item = state["arms"]["evoskill"]
    total = len(runner.stream)
    save()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            while len(item["rows"]) < total or item["pending_update"] is not None:
                if (root / "PAUSE").exists():
                    save("paused")
                    return
                index = item["pending_update"] if item["pending_update"] is not None else len(item["rows"])
                phase = "rsi_update" if item["pending_update"] is not None else "formal"
                state["active"] = {"evoskill": {"phase": phase, "index": index, "started": time.time()}}
                item["status"] = "running"
                save()
                output = root / "arms/evoskill/steps" / f"{index:06d}"
                if phase == "formal":
                    future = pool.submit(runner.run, "evoskill", index, output / "formal", skills=Path(item["skills"]))
                else:
                    future = pool.submit(lambda: asyncio.run(update(runner, index, item["workspace"], output / "update")))
                while not future.done():
                    atomic(root / "heartbeat.json", {"time": time.time(), "pid": os.getpid(), "active": state["active"]})
                    save()
                    time.sleep(10)
                try:
                    value = future.result()
                    if phase == "formal":
                        if value.get("exception"):
                            raise RuntimeError("formal agent execution exception; preserve and review")
                        if not item["rows"]:
                            acceptance(root, runner, value)
                        item["rows"].append(value)
                        if len(item["rows"]) % config["arms"]["evoskill"]["update_every_completed_tasks"] == 0 and len(item["rows"]) < total:
                            item["pending_update"] = index
                    else:
                        if not value.get("execution", {}).get("passed"):
                            raise RuntimeError("native update lacks verified iteration/sample execution")
                        item["updates"].append(value)
                        item.update(skills=value["skills"], workspace=value["workspace"], pending_update=None)
                        if len(item["updates"]) == 1:
                            samples = [e["data"] for e in value["events"] if e["event"] == "sample"]
                            evaluated = any(e["event"] == "eval_result" for e in value["events"])
                            passed = evaluated or bool(samples) and all(s["passed"] for s in samples)
                            atomic(root / "first-update-acceptance.json", {"passed": passed,
                                "native_loop_completed": True, "deployed_skill_hashes": value["skills_hashes"],
                                "support_tasks": value["support_tasks"], "rollouts": len(value["rollouts"]),
                                "best_program": value["best_program"], "time": time.time()})
                            if not passed:
                                raise RuntimeError("first native update produced no evaluable candidate from failed samples")
                    item["status"] = "ready"
                    item.pop("error", None)
                except NetworkUnavailable as exc:
                    item.update(status="waiting_for_network", error=str(exc))
                    save("waiting_for_network")
                    time.sleep(config["runtime"]["network_recovery_cooldown_seconds"])
                    continue
                except BaseException as exc:
                    item.update(status="budget_exhausted" if isinstance(exc, SpendBlocked) else "needs_review",
                                error={"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
                    save(item["status"])
                    return
                state["active"] = {}
                save()
        save("completed")
    finally:
        server.shutdown()
        server.server_close()
        fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    run(parser.parse_args().root.resolve())
