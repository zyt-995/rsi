#!/usr/bin/env python3
"""Summarize main-trial rewards/costs from the overnight Terminal run."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


ARMS = ("static", "fixed-loop", "self-action", "open-ended", "evolution")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize_arm(root: Path, arm: str) -> dict[str, object]:
    trials: list[dict[str, object]] = []
    for result_path in sorted(root.glob(f"rep-*/{arm}/*/result.json")):
        trial_dir = result_path.parent
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            trials.append({"trial_dir": str(trial_dir), "parse_error": str(exc)})
            continue
        trajectory_path = trial_dir / "agent" / "trajectory.json"
        try:
            trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            trajectory = {}
        metrics = trajectory.get("final_metrics") or {}
        reward = (((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"))
        started = parse_time(result.get("started_at"))
        finished = parse_time(result.get("finished_at"))
        trials.append(
            {
                "trial_dir": str(trial_dir),
                "task": trial_dir.name.rpartition("__")[0],
                "reward": float(reward) if reward is not None else None,
                "input_tokens": int(metrics.get("total_prompt_tokens", 0)),
                "output_tokens": int(metrics.get("total_completion_tokens", 0)),
                "cached_tokens": int(metrics.get("total_cached_tokens", 0)),
                "wall_sec": (finished - started).total_seconds() if started and finished else None,
                "exception": result.get("exception_info"),
            }
        )
    valid_rewards = [float(item["reward"]) for item in trials if item.get("reward") is not None]
    wall = [float(item["wall_sec"]) for item in trials if item.get("wall_sec") is not None]
    return {
        "arm": arm,
        "n_trials": len(trials),
        "n_valid_rewards": len(valid_rewards),
        "passes": sum(value > 0 for value in valid_rewards),
        "reward_sum": sum(valid_rewards),
        "mean_reward": sum(valid_rewards) / len(valid_rewards) if valid_rewards else None,
        "exceptions": sum(bool(item.get("exception")) for item in trials),
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in trials),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in trials),
        "wall_sec": sum(wall),
        "trials": trials,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    value = {
        "root": str(root),
        "note": "Main task trials only; open-ended/evolution shadow replay cost is stored separately under underscore-prefixed directories.",
        "arms": {arm: summarize_arm(root, arm) for arm in ARMS},
    }
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
