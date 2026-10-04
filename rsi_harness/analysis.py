from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Iterable

from .state import write_json


RUN_FIELDS = (
    "run", "benchmark", "mode", "seed", "status", "tasks", "attempted", "completed",
    "cumulative_reward", "mean_reward", "oracle_regret", "state_changes", "program_changes",
    "invalid_actions", "total_units", "self_units", "backend_calls", "program_runs",
    "input_tokens", "output_tokens", "token_usage_complete", "candidates", "selected_candidates",
    "accepted_updates", "rejected_updates", "stale_answers", "reward_gain_vs_static",
    "gain_per_self_unit",
)


def discover_runs(inputs: Iterable[Path]) -> list[Path]:
    found: set[Path] = set()
    for raw in inputs:
        path = Path(raw).resolve()
        if (path / "manifest.json").is_file() and (path / "summary.json").is_file():
            found.add(path)
        if path.is_dir():
            for summary in path.rglob("summary.json"):
                if (summary.parent / "manifest.json").is_file():
                    found.add(summary.parent.resolve())
    return sorted(found)


def _events(path: Path) -> list[dict]:
    events = []
    event_path = path / "events.jsonl"
    if not event_path.is_file():
        return events
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def collect(run_dirs: Iterable[Path]) -> tuple[list[dict], list[dict], dict]:
    runs, tasks = [], []
    curves: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for run_dir in run_dirs:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        task_rows = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
        events = _events(run_dir)
        cost = summary.get("cost", {})
        seed = manifest.get("benchmark", {}).get("seed")
        candidate_events = [event for event in events if event.get("event") == "candidate_validated"]
        state_events = [event for event in events if event.get("event") == "state_candidate_validated"]
        update_events = candidate_events + state_events
        record = {
            "run": str(run_dir), "benchmark": summary.get("benchmark"),
            "mode": summary.get("mode"), "seed": seed, "status": summary.get("status"),
            **{key: summary.get(key) for key in (
                "tasks", "attempted", "completed", "cumulative_reward", "mean_reward",
                "oracle_regret", "state_changes", "program_changes", "invalid_actions",
            )},
            **{key: cost.get(key) for key in (
                "total_units", "self_units", "backend_calls", "program_runs", "input_tokens",
                "output_tokens", "token_usage_complete",
            )},
            "candidates": len(candidate_events),
            "selected_candidates": sum(
                event.get("event") == "candidate_selected" for event in events
            ),
            "accepted_updates": sum(bool(event.get("result", {}).get("passed"))
                                    for event in update_events),
            "rejected_updates": sum(not bool(event.get("result", {}).get("passed"))
                                    for event in update_events),
            "stale_answers": sum(
                bool(event.get("result", {}).get("feedback", {}).get("stale_answer"))
                for event in events if event.get("event") == "action"
            ),
        }
        runs.append(record)
        cumulative = 0.0
        for index, row in enumerate(task_rows):
            cumulative += float(row.get("reward", 0.0))
            flat = {
                "run": str(run_dir), "benchmark": summary.get("benchmark"),
                "mode": summary.get("mode"), "seed": seed, "task_index": index,
                **row, "cumulative_reward": cumulative,
            }
            flat.update({f"evaluation_{key}": value for key, value in row.get("evaluation", {}).items()})
            flat.pop("evaluation", None)
            tasks.append(flat)
            curves[str(summary.get("mode"))][index].append(float(row.get("reward", 0.0)))
    static_rewards = {
        (run["benchmark"], run["seed"]): float(run["cumulative_reward"] or 0)
        for run in runs if run["mode"] == "static"
    }
    for run in runs:
        baseline = static_rewards.get((run["benchmark"], run["seed"]))
        gain = None if baseline is None else float(run["cumulative_reward"] or 0) - baseline
        run["reward_gain_vs_static"] = gain
        run["gain_per_self_unit"] = (
            gain / float(run["self_units"]) if gain is not None and run["self_units"] else None
        )
    by_mode = {}
    for mode in sorted({str(run["mode"]) for run in runs}):
        group = [run for run in runs if str(run["mode"]) == mode]
        by_mode[mode] = {
            "runs": len(group),
            "mean_cumulative_reward": mean(float(item["cumulative_reward"] or 0) for item in group),
            "mean_total_units": mean(float(item["total_units"] or 0) for item in group),
            "mean_self_units": mean(float(item["self_units"] or 0) for item in group),
            "state_changes": sum(int(item["state_changes"] or 0) for item in group),
            "program_changes": sum(int(item["program_changes"] or 0) for item in group),
            "candidates": sum(int(item["candidates"] or 0) for item in group),
            "accepted_updates": sum(int(item["accepted_updates"] or 0) for item in group),
            "rejected_updates": sum(int(item["rejected_updates"] or 0) for item in group),
            "stale_answers": sum(int(item["stale_answers"] or 0) for item in group),
            "mean_reward_gain_vs_static": (
                mean(float(item["reward_gain_vs_static"]) for item in group
                     if item["reward_gain_vs_static"] is not None)
                if any(item["reward_gain_vs_static"] is not None for item in group) else None
            ),
            "mean_gain_per_self_unit": (
                mean(float(item["gain_per_self_unit"]) for item in group
                     if item["gain_per_self_unit"] is not None)
                if any(item["gain_per_self_unit"] is not None for item in group) else None
            ),
        }
    aggregate = {
        "schema_version": 1, "runs": len(runs), "by_mode": by_mode,
        "learning_curves": {
            mode: [{"task_index": index, "mean_reward": mean(values)}
                   for index, values in sorted(points.items())]
            for mode, points in sorted(curves.items())
        },
    }
    return runs, tasks, aggregate


def _write_csv(path: Path, rows: list[dict], fields: Iterable[str] | None = None) -> None:
    if fields is None:
        fields = sorted({key for row in rows for key in row})
    fields = list(fields)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _svg(aggregate: dict) -> str:
    width, height = 960, 520
    left, top, plot_w, plot_h = 70, 35, 850, 400
    curves = aggregate["learning_curves"]
    max_x = max((point["task_index"] for points in curves.values() for point in points), default=1)
    colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#111"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#111"/>',
        f'<text x="{width / 2}" y="500" text-anchor="middle" font-family="sans-serif">Task index</text>',
        f'<text x="18" y="{top + plot_h / 2}" transform="rotate(-90 18 {top + plot_h / 2})" text-anchor="middle" font-family="sans-serif">Mean reward</text>',
    ]
    for tick in range(6):
        reward = tick / 5
        y = top + plot_h * (1 - reward)
        lines.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e5e7eb"/>',
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12">{reward:.1f}</text>',
        ])
    for number, (mode, points) in enumerate(sorted(curves.items())):
        color = colors[number % len(colors)]
        coords = []
        for point in points:
            x = left + plot_w * point["task_index"] / max(max_x, 1)
            y = top + plot_h * (1 - point["mean_reward"])
            coords.append(f"{x:.1f},{y:.1f}")
        if coords:
            lines.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        legend_y = top + 18 * number
        lines.append(f'<text x="{left + 10}" y="{legend_y}" fill="{color}" font-family="sans-serif" font-size="13">{mode}</text>')
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def analyze(inputs: Iterable[Path], output: Path) -> dict:
    run_dirs = discover_runs(inputs)
    if not run_dirs:
        raise ValueError("no complete run directories found")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    runs, tasks, aggregate = collect(run_dirs)
    write_json(output / "aggregate.json", aggregate)
    _write_csv(output / "runs.csv", runs, RUN_FIELDS)
    _write_csv(output / "tasks.csv", tasks)
    (output / "learning-curves.svg").write_text(_svg(aggregate), encoding="utf-8")
    return aggregate
