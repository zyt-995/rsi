"""Independent synthetic Harbor acceptance; NEVER contributes to formal scores."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi_harness import clean_campaign as core
from rsi_harness import meta_skill as method
from rsi_harness.meta_skill_campaign import code_snapshot, exclusive
from rsi_harness.clean_rsi import digest, initialize, read_json
from rsi_harness.terminal_infra import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8040)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "acceptance.json").exists():
        raise RuntimeError("Use a fresh numbered acceptance directory; never overwrite an attempt")
    with exclusive(root / "acceptance.lock"):
        code_hash = digest(code_snapshot())
        store = root / "state"
        initialize(store, "skill-rsi")
        results = []
        for index, name in enumerate(("clean-rsi-a", "clean-rsi-b")):
            task = core.PROJECT / "benchmarks/smoke" / name
            folder = root / name
            folder.mkdir(parents=True, exist_ok=True)
            records = list((folder / "trials").glob("*/result.json"))
            if not records:
                if (folder / "launch.json").exists():
                    raise RuntimeError("interrupted smoke is not silently repeated")
                command = core.harbor_command(task, folder / "trials", "skill-rsi", store)
                command = [s.replace('"max_tokens": 32768', '"max_tokens": 2048')
                           .replace('"timeout": 2700', '"timeout": 600')
                           .replace("max_turns=15", "max_turns=5") for s in command]
                command[command.index("--agent-timeout") + 1] = "900"
                atomic_json(folder / "launch.json", {"command": command, "formal": False})
                with (folder / "worker.log").open("ab", buffering=0) as stream:
                    subprocess.run(command, cwd=core.PROJECT, env=core.runner_environment(args.port),
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=1800, check=False)
                records = list((folder / "trials").glob("*/result.json"))
            if len(records) != 1:
                raise RuntimeError("missing or ambiguous smoke result")
            path = records[0]
            valid, reason = core.scored_result(path)
            if not valid:
                raise RuntimeError("invalid smoke:" + str(reason))
            reward = read_json(path)["verifier_result"]["rewards"]["reward"]
            runtime = read_json(path.parent / "agent/rsi_end.json", {})
            results.append({"task": name, "result": str(path), "reward": reward, "runtime": runtime})
            if index == 0:
                results[-1]["improvement"] = method.improve(store, name, (task / "instruction.md").read_text(),
                    path.parent, reward, f"http://127.0.0.1:{args.port}/v1", core.MODEL)
            atomic_json(root / "progress.json", {"formal": False, "results": results})
        update = results[0]["improvement"]
        passed = (all(r["reward"] == 1 for r in results) and update["accepted"]
                  and update["accounting"]["calls"] <= 3
                  and results[1]["runtime"].get("version") == update["after"]
                  and bool(results[1]["runtime"].get("selected_skills"))
                  and digest(code_snapshot()) == code_hash)
        report = {"formal": False, "passed": passed, "code_sha256": code_hash, "results": results,
                  "scope": "technical version consumption on synthetic tasks, not evidence of transfer gain"}
        atomic_json(root / "acceptance.json", report)
        print(json.dumps({"passed": passed, "rewards": [r["reward"] for r in results],
                          "update_accepted": update["accepted"], "update_reason": update["reason"],
                          "selected_skills": results[1]["runtime"].get("selected_skills")}, indent=2))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
