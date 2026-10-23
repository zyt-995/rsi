"""Native RSI worker using the same normalized scoring as formal tasks."""
import argparse
import os
from pathlib import Path
import subprocess
import time

from swe_chain_evo.common import ROOT, PROJECT, PYTHON, atomic, read, existing_imports
existing_imports()
from methods.evo_fifty.update_worker import RemoteBroker, error_record
from methods.evo_fifty.spend import SpendBlocked
from methods.evo_fifty.trial import NetworkUnavailable
from methods.evo_fifty.gateway import transport_failure_record
from .trial import TrialRunner


def transport_checkpoint(runner, arm, error):
    eligible = error["type"] == "NetworkUnavailable" or (
        arm == "meta-harness" and "native proposer provider accounting requires review" in error["message"])
    if not eligible:
        return False
    uncertain = []
    for path in (runner.root / "provider-calls").glob("*/request.json"):
        row = read(path)
        if row["arm"] != arm:
            continue
        if row["state"] == "started":
            return False
        if row["state"] in ("requires_review", "rejected") or row.get("retryable_transport"):
            response = path.with_name("response.json")
            if not transport_failure_record(row, read(response) if response.exists() else None):
                return False
            uncertain.append(row["id"])
    return bool(uncertain)


def update_subprocess(runner, arm, index, control_token):
    output = runner.root / "arms" / arm / "steps" / f"{index:06d}" / "update"
    output.mkdir(parents=True, exist_ok=True)
    outcome = output / "worker-outcome.json"
    if outcome.exists():
        data = read(outcome)
        if data["ok"]:
            return data["decision"]
        if not transport_checkpoint(runner, arm, data["error"]):
            raise RuntimeError("failed paid update retained; inspect " + str(outcome))
        archive = output / "transport-worker-checkpoints"
        archive.mkdir(exist_ok=True)
        count = len(list(archive.glob("*.json")))
        if count >= runner.config["runtime"].get("native_session_resume_limit", 5):
            raise RuntimeError("native transport continuation limit reached; inspect " + str(outcome))
        # Only the worker outcome is archived. Native sessions, modules, trial
        # results, scopes and all charged/uncertain receipts remain in place.
        outcome.replace(archive / f"{count:03d}.json")
    env = dict(runner.env)
    for key in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    env.update(EVO_ONLINE_CONTROL_TOKEN=control_token,
        PYTHONPATH=os.pathsep.join([str(ROOT), str(PROJECT)]),
        TMPDIR=str(runner.root / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    cmd = [str(PYTHON), "-u", "-m", "formal_swe_chain.update_worker", "--root", str(runner.root),
           "--arm", arm, "--index", str(index)]
    with (output / "coordinator-update-worker.log").open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT)
        atomic(output / "worker-launch.json", {"pid": proc.pid, "time": time.time(), "arm": arm, "index": index})
        proc.wait()
    if not outcome.exists():
        raise RuntimeError("native update exited without checkpoint: " + str(output))
    data = read(outcome)
    if data["ok"]:
        return data["decision"]
    error = data["error"]
    if error["type"] == "SpendBlocked" or "global dollar limit" in error["message"]:
        raise SpendBlocked(error["message"])
    if error["type"] == "NetworkUnavailable":
        raise NetworkUnavailable(error["message"])
    if transport_checkpoint(runner, arm, error):
        raise NetworkUnavailable(error["message"])
    raise RuntimeError(error["type"] + ": " + error["message"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--arm", choices=("meta-harness", "modularrsi"), required=True)
    parser.add_argument("--index", type=int, required=True)
    args = parser.parse_args()
    token = os.environ.pop("EVO_ONLINE_CONTROL_TOKEN")
    config = read(args.root / "configuration.json")
    broker = RemoteBroker(args.root, args.arm, config["runtime"]["gateway_port"], token)
    runner = TrialRunner(args.root, broker, broker.port)
    runner.pins = read(args.root / "native-source-pins.json")
    state = read(args.root / "native-campaign.json")["arms"][args.arm]
    output = args.root / "arms" / args.arm / "steps" / f"{args.index:06d}" / "update"
    try:
        from swe_chain_evo.native_compat import meta_update, modular_update
        operator = meta_update if args.arm == "meta-harness" else modular_update
        decision = operator(runner, args.index, Path(state["parent"]), state["rows"], output)
        value = {"ok": True, "decision": decision}
    except BaseException as exc:
        value = {"ok": False, "error": error_record(exc)}
    atomic(output / "worker-outcome.json", value)
    if not value["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
