"""Receipt-based progress and matched-prefix reports for a formal chain."""
import csv
import io
from pathlib import Path
import re
import time

from swe_chain_evo.common import atomic, read

ARMS = ("kira-static", "meta-harness")


def step_of(call):
    scope = call["scope"]
    match = re.search(r"(?:^|/)steps/(\d+)(?:/|$)", scope)
    if not match:
        match = re.search(r"(?:^|/)evo_(\d+)(?:/|$)", scope)
    return int(match[1]) if match else None


def usage(calls):
    return {"input": sum(c["usage"]["prompt_tokens"] for c in calls),
            "output": sum(c["usage"]["completion_tokens"] for c in calls)}


def summarize(arm, rows, receipts, total):
    own = [r for r in receipts if r["arm"] == arm]
    calls = [r for r in own if r["state"] == "settled"]
    task = [r for r in calls if r["phase"] == "formal"]
    rsi = [r for r in calls if r["phase"] != "formal"]
    pending = [r["id"] for r in own if r["state"] == "started"]
    unknown = [r["id"] for r in own if r["state"] == "requires_review"]
    full = len(rows) == total
    prefix = 0
    for row in rows:
        if row["reward"] != 1:
            break
        prefix += 1
    return {"method": arm, "completed": len(rows), "passed": sum(r["reward"] for r in rows),
        "step_accuracy": sum(r["reward"] for r in rows) / len(rows) if rows else None,
        "chain_accuracy": int(all(r["reward"] == 1 for r in rows)) if full else None,
        "official_prefix_chain_score": prefix / total if full else None,
        "complete_chains": int(full), "task_tokens": usage(task), "rsi_tokens": usage(rsi),
        "total_tokens": usage(calls), "cost_usd": sum(r["usage"]["cost"] for r in calls),
        "usage_complete": not unknown and not pending, "unknown_usage_calls": unknown,
        "pending_calls": pending, "task_exceptions": sum(bool(r.get("exception")) for r in rows)}


def metrics(root, state, snapshot):
    receipts = [read(p) for p in (root / "provider-calls").glob("*/request.json")]
    stream = read(root / "task-stream.json")
    total = len(stream)
    common = min(len(state["arms"][a]["rows"]) for a in ARMS)
    progress, matched = [], []
    scope_issues = [r["id"] for r in receipts if step_of(r) is None]
    for arm in ARMS:
        item = state["arms"][arm]
        value = summarize(arm, item["rows"], receipts, total)
        value.update(status=item["status"], active=state.get("active", {}).get(arm))
        progress.append(value)
        # An update after the last matched task has not yet affected any score
        # in this prefix. Its cost is retained in progress and enters the
        # matched comparison once a later formal outcome is available.
        selected = [r for r in receipts if step_of(r) is not None
            and step_of(r) < (common if r["phase"] == "formal" else common - 1)]
        matched.append(summarize(arm, item["rows"][:common], selected, total))
    return {"schema": "swe-chain-evo-formal-metrics-v1", "development_only": False,
        "updated": time.time(), "state": state["state"], "chain_id": stream[0]["chain_id"],
        "tasks_per_arm": total, "arms": progress, "matched_completed_tasks": common,
        "matched_prefix": matched, "unmapped_receipt_ids": scope_issues,
        "comparison_note": "common task prefix; post-prefix updates excluded until a subsequent score",
        "equal_total_token_budget_comparison": False, "spend": snapshot}


def write_report(root, state, broker):
    value = metrics(root, state, broker.ledger.snapshot())
    atomic(root / "metrics.json", value)
    buffer = io.StringIO()
    fields = ["method", "completed", "passed", "step_accuracy", "chain_accuracy",
              "task_input_tokens", "task_output_tokens", "rsi_input_tokens", "rsi_output_tokens",
              "total_tokens", "cost_usd", "usage_complete"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in value["matched_prefix"]:
        record = {k: row[k] for k in fields if k in row}
        for phase in ("task", "rsi"):
            for direction in ("input", "output"):
                record[f"{phase}_{direction}_tokens"] = row[f"{phase}_tokens"][direction]
        record["total_tokens"] = sum(row["total_tokens"].values())
        writer.writerow(record)
    target = root / "results.csv"
    temp = target.with_suffix(".csv.tmp")
    temp.write_text(buffer.getvalue())
    temp.replace(target)
    return value


def plots(root, value):
    if not value["matched_completed_tasks"]:
        return
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    colors = ("#71717a", "#2563eb", "#a16207", "#059669")
    for phase in ("task", "total"):
        fig, ax = plt.subplots(figsize=(8, 5), layout="constrained")
        for row, color in zip(value["matched_prefix"], colors):
            x = sum(row[f"{phase}_tokens"].values()) / 1e6
            y = 100 * row["step_accuracy"]
            ax.scatter(x, y, color=color, s=70, label=row["method"])
        ax.set(xlabel=f"{phase.title()} tokens (millions, input + output)", ylabel="Step accuracy (%)",
               title=f"Common completed prefix: {value['matched_completed_tasks']}/{value['tasks_per_arm']} tasks",
               ylim=(-3, 103))
        ax.grid(alpha=.2)
        ax.legend()
        fig.savefig(root / f"accuracy-vs-{phase}-tokens.png", dpi=160)
        fig.savefig(root / f"accuracy-vs-{phase}-tokens.svg")
        plt.close(fig)
