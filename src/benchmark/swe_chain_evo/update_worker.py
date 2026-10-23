"""Separate process per native RSI implementation; shared gateway/accounting."""
import argparse
import os
from pathlib import Path

from .common import existing_imports, read, atomic
existing_imports()
from methods.evo_fifty.update_worker import RemoteBroker, error_record
from .trial import TrialRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--arm", choices=("meta-harness", "modularrsi"), required=True)
    parser.add_argument("--index", type=int, required=True)
    args = parser.parse_args()
    token = os.environ.pop("EVO_ONLINE_CONTROL_TOKEN")
    config = read(args.root / "configuration.json")
    broker = RemoteBroker(args.root, args.arm, config["runtime"]["gateway_port"], token)
    runner = TrialRunner(args.root, broker, port=broker.port)
    runner.pins = read(args.root / "native-source-pins.json")
    state = read(args.root / "native-campaign.json")["arms"][args.arm]
    output = args.root / "arms" / args.arm / "steps" / f"{args.index:06d}" / "update"
    try:
        if args.arm == "meta-harness":
            from .native_compat import meta_update as update
        else:
            from .native_compat import modular_update as update
        decision = update(runner, args.index, Path(state["parent"]), state["rows"], output)
        outcome = {"ok": True, "decision": decision}
    except BaseException as exc:
        outcome = {"ok": False, "error": error_record(exc)}
    atomic(output / "worker-outcome.json", outcome)
    if not outcome["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
