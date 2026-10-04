"""Run an already model-generated controller on an isolated synthetic task.

This is development acceptance, never formal data or initial campaign state.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.clean_campaign import harbor_command, runner_environment, scored_result
from rsi_harness.clean_rsi import atomic_json, read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8041)
    args = parser.parse_args()
    root = args.root.resolve()
    store = root / "state"
    head = read_json(store / "HEAD.json")
    if not head or head["revision"] < 1:
        raise RuntimeError("requires an accepted, model-generated revised controller")
    folder = root / "native-smoke"
    folder.mkdir(parents=True, exist_ok=True)
    records = list((folder / "trials").glob("*/result.json"))
    if not records:
        if (folder / "launch.json").exists():
            raise RuntimeError("interrupted smoke is not automatically repeated")
        command = harbor_command(
            PROJECT / "benchmarks/smoke/clean-rsi-controller",
            folder / "trials",
            "harness-rsi",
            store,
        )
        command = [
            s.replace('"max_tokens": 32768', '"max_tokens": 2048')
            .replace('"timeout": 2700', '"timeout": 600')
            .replace("max_turns=15", "max_turns=5")
            for s in command
        ]
        command[command.index("--agent-timeout") + 1] = "900"
        atomic_json(
            folder / "launch.json",
            {"formal": False, "command": command, "version": head},
        )
        with (folder / "worker.log").open("ab", buffering=0) as stream:
            subprocess.run(
                command,
                cwd=PROJECT,
                env=runner_environment(args.port),
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        records = list((folder / "trials").glob("*/result.json"))
    if len(records) != 1:
        raise RuntimeError("expected one completed smoke trial")
    result = records[0]
    valid, reason = scored_result(result)
    meta = read_json(result.parent / "agent/rsi_end.json", {})
    event_path = result.parent / "agent/rsi_events.jsonl"
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    changed = [
        e
        for e in events
        if e.get("event") == "controller_hook"
        and e.get("changed")
        and e.get("hook") != "before_model"
    ]
    reward = (
        (read_json(result).get("verifier_result") or {})
        .get("rewards", {})
        .get("reward")
    )
    passed = (
        valid
        and reward == 1
        and meta.get("version") == head
        and not meta.get("controller_runtime_error")
        and bool(changed)
    )
    report = {
        "formal": False,
        "passed": passed,
        "valid": valid,
        "reason": reason,
        "reward": reward,
        "result": str(result),
        "runtime": meta,
        "changed_control_hooks": len(changed),
    }
    atomic_json(root / "acceptance.json", report)
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
