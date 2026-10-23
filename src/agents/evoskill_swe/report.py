"""Combine receipt-based metrics without changing existing experiments."""
import csv
import io
from pathlib import Path
import time

from . import RELEASE
from swe_chain_evo.common import atomic, read
from formal_swe_chain.report import summarize, step_of


def selected(receipts, completed):
    return [r for r in receipts if step_of(r) is not None
            and step_of(r) < (completed if r["phase"] == "formal" else completed - 1)]


def report(root, state, broker):
    source = Path(broker.config["comparison_source"]["run"])
    old = read(source / "native-campaign.json")
    own_receipts = [read(p) for p in (root / "provider-calls").glob("*/request.json")]
    previous_receipts = [read(p) for p in (source / "provider-calls").glob("*/request.json")]
    arms = {**old["arms"], **state["arms"]}
    receipts = previous_receipts + own_receipts
    # UUID call IDs are the deduplication boundary, even if logs are reused.
    if len({r["id"] for r in receipts}) != len(receipts):
        raise RuntimeError("duplicate provider call in combined comparison")
    total = len(read(root / "task-stream.json"))
    common = min(len(a["rows"]) for a in arms.values())
    progress, matched = [], []
    for name, arm in arms.items():
        value = summarize(name, arm["rows"], receipts, total)
        value.update(status=arm["status"], active=(state if name == "evoskill" else old).get("active", {}).get(name))
        progress.append(value)
        matched.append(summarize(name, arm["rows"][:common], selected(receipts, common), total))
    value = {"schema": "swe-chain-evo-five-arm-comparison-v1", "updated": time.time(),
        "state": state["state"], "tasks_per_arm": total, "arms": progress,
        "matched_completed_tasks": common, "matched_prefix": matched,
        "equal_total_token_budget_comparison": False,
        "comparison_note": "same tasks/common completed prefix and solve envelope; method-native update workloads differ",
        "spend": broker.ledger.snapshot(), "source_run": str(source), "evoskill_run": str(root)}
    atomic(root / "metrics.json", value)
    fields = ["method", "completed", "passed", "step_accuracy", "chain_accuracy",
              "task_input_tokens", "task_output_tokens", "rsi_input_tokens", "rsi_output_tokens",
              "total_tokens", "cost_usd", "usage_complete"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in matched:
        record = {k: row[k] for k in fields if k in row}
        for phase in ("task", "rsi"):
            for direction in ("input", "output"):
                record[f"{phase}_{direction}_tokens"] = row[f"{phase}_tokens"][direction]
        record["total_tokens"] = sum(row["total_tokens"].values())
        writer.writerow(record)
    (root / "results.csv.tmp").write_text(buffer.getvalue())
    (root / "results.csv.tmp").replace(root / "results.csv")
    return value


def plots(root, value):
    if not value["matched_completed_tasks"]:
        return
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    colors = ("#71717a", "#2563eb", "#a16207", "#059669", "#9333ea")
    for phase in ("task", "total"):
        fig, ax = plt.subplots(figsize=(8, 5), layout="constrained")
        for row, color in zip(value["matched_prefix"], colors):
            ax.scatter(sum(row[f"{phase}_tokens"].values()) / 1e6,
                       100 * row["step_accuracy"], color=color, s=70, label=row["method"])
        ax.set(xlabel=f"{phase.title()} tokens (millions, input + output)", ylabel="Step accuracy (%)",
               title=f"Common completed prefix: {value['matched_completed_tasks']}/{value['tasks_per_arm']} tasks",
               ylim=(-3, 103))
        ax.grid(alpha=.2)
        ax.legend()
        for ext in ("png", "svg"):
            fig.savefig(root / f"accuracy-vs-{phase}-tokens.{ext}", dpi=160)
        plt.close(fig)
