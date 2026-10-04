"""Read-only report for the clean campaign; never starts or repeats work."""

import argparse
import json
import time
from pathlib import Path


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "runs/clean-rsi-20260923-qwen38-27b",
    )
    args = parser.parse_args()
    status = read(args.root / "status.json")
    if not status:
        raise SystemExit("No campaign status exists yet")
    report = {
        "root": str(args.root.resolve()),
        "heartbeat_age_seconds": round(time.time() - status["time"], 1),
        "phase": status["phase"],
        "terminal": status.get("terminal"),
        "summary": status["summary"],
        "paired": status.get("paired"),
        "scheduling": status.get("scheduling"),
        "pilot_gate": status["pilot_gate"],
        "endpoints": status.get("endpoints"),
        "blocked": status["blocked"],
        "waiting": status["waiting"],
        "active": [],
    }
    for port, worker in status["active"].items():
        folder = args.root / worker["chain"] / worker["arm"] / "stages" / worker["task"]
        events = list((folder / "trials").glob("*/agent/rsi_events.jsonl"))
        row = {
            "port": port,
            **worker,
            "elapsed_minutes": round((time.time() - worker["started"]) / 60, 1),
        }
        if events:
            records = [json.loads(line) for line in events[0].read_text().splitlines()]
            if records:
                row["last_event"] = records[-1]["event"]
                row["last_event_age_seconds"] = round(
                    time.time() - records[-1]["time"], 1
                )
            row["model_calls_started"] = sum(
                e["event"] == "model_call" for e in records
            )
            row["controller_errors"] = sum(
                e["event"] == "controller_error" for e in records
            )
        report["active"].append(row)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
