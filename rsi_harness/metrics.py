from __future__ import annotations

import csv
from pathlib import Path


def summarize(rows: list[dict], costs: dict, recovery_streak: int = 2) -> dict:
    """Recovery: first start of N consecutive full-reward tasks in the new regime.

    No recovery before the next shift is right-censored (null), never set to zero.
    Oracle regret is reported only when the adapter explicitly provides an oracle.
    """
    recovery = []
    boundaries = [i for i, row in enumerate(rows) if row["evaluation"].get("change_point")]
    for n, start in enumerate(boundaries):
        end = boundaries[n + 1] if n + 1 < len(boundaries) else len(rows)
        recovered_at = None
        for i in range(start, end - recovery_streak + 1):
            window = rows[i:i + recovery_streak]
            if all(row["completed"] and row["reward"] >= 1.0 for row in window):
                recovered_at = i - start
                break
        recovery.append({"task_id": rows[start]["task_id"], "regime": rows[start]["evaluation"].get("regime"),
                         "tasks_to_recover": recovered_at, "censored": recovered_at is None})
    oracle_available = all("oracle_reward" in row["evaluation"] for row in rows)
    return {
        "tasks": len(rows), "attempted": sum(r["attempted"] for r in rows),
        "completed": sum(r["completed"] for r in rows),
        "cumulative_reward": sum(r["reward"] for r in rows),
        "mean_reward": sum(r["reward"] for r in rows) / len(rows) if rows else 0.0,
        "oracle_regret": (sum(r["evaluation"]["oracle_reward"] - r["reward"] for r in rows)
                          if oracle_available else None),
        "recovery_definition": {"threshold": 1.0, "consecutive_tasks": recovery_streak, "offset_is_zero_based": True},
        "recovery": recovery, "cost": costs,
        "state_changes": sum(r["state_changes"] for r in rows),
        "program_changes": sum(r.get("program_changes", 0) for r in rows),
        "invalid_actions": sum(r["invalid_actions"] for r in rows),
    }


def write_task_csv(path: Path, rows: list[dict]) -> None:
    fields = ["task_id", "index", "regime", "change_point", "attempted", "completed",
              "reward", "status", "task_units", "self_units", "state_changes", "program_changes",
              "invalid_actions", "revision", "program_revision"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            flat = {**row, **row["evaluation"]}
            writer.writerow({key: flat.get(key) for key in fields})
