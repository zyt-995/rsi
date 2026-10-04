"""Real model + native Harbor + sequential version-consumption technical acceptance.

Uses synthetic tasks and a smaller solve cap; never mixed into formal metrics.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi_harness.clean_campaign import (
    MODEL,
    PROJECT,
    harbor_command,
    runner_environment,
    scored_result,
)
from rsi_harness.clean_rsi import atomic_json, improve, initialize, read_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8044)
    parser.add_argument("--arm", choices=("skill-rsi", "harness-rsi"), required=True)
    args = parser.parse_args()
    root = args.root.resolve() / args.arm
    store = root / "state"
    initialize(store, args.arm)
    results = []
    for index, name in enumerate(("clean-rsi-a", "clean-rsi-b")):
        task = PROJECT / "benchmarks/smoke" / name
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        records = list((folder / "trials").glob("*/result.json"))
        if not records:
            command = harbor_command(task, folder / "trials", args.arm, store)
            command = [
                s.replace('"max_tokens": 32768', '"max_tokens": 2048')
                .replace('"timeout": 2700', '"timeout": 600')
                .replace("max_turns=15", "max_turns=5")
                for s in command
            ]
            command[command.index("--agent-timeout") + 1] = "900"
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
            raise RuntimeError(f"expected one result: {folder}")
        record = records[0]
        valid, reason = scored_result(record)
        if not valid:
            raise RuntimeError(f"invalid smoke trial: {record}: {reason}")
        reward = read_json(record)["verifier_result"]["rewards"]["reward"]
        end = read_json(record.parent / "agent/rsi_end.json")
        results.append(
            {"task": name, "reward": reward, "result": str(record), "runtime": end}
        )
        if index == 0:
            results[-1]["improvement"] = improve(
                store,
                args.arm,
                name,
                (task / "instruction.md").read_text(),
                record.parent,
                reward,
                f"http://127.0.0.1:{args.port}/v1",
                MODEL,
            )
        atomic_json(root / "acceptance.json", {"formal": False, "results": results})
    passed = (
        all(r["reward"] == 1 for r in results) and results[0]["improvement"]["accepted"]
    )
    passed = passed and results[1]["runtime"]["version"]["revision"] > 0
    passed = passed and not any(
        r["runtime"]["controller_runtime_error"] for r in results
    )
    if args.arm == "skill-rsi":
        passed = passed and bool(results[1]["runtime"]["selected_skills"])
    else:
        passed = passed and results[1]["runtime"]["controller_hooks"] > 0
    report = {"formal": False, "passed": passed, "results": results}
    atomic_json(root / "acceptance.json", report)
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
