"""Receipt-based per-chain and matched-task reports; safe before launch."""
import argparse
import csv
import io
from pathlib import Path

from swe_chain_evo.common import atomic, read
from formal_swe_chain.report import step_of
from . import SESSION

_RECEIPT_CACHE = {}


def load_receipts(root):
    """Cache projected receipts; do not repeatedly parse archived giant prompts."""
    folder = Path(root) / "provider-calls"
    cache = _RECEIPT_CACHE.setdefault(str(folder), {})
    values = []
    for path in folder.glob("*/request.json"):
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        previous = cache.get(str(path))
        if previous is None or previous[0] != signature:
            raw = read(path)
            value = {key: raw[key] for key in ("id", "arm", "phase", "scope", "state")}
            value["usage"] = raw.get("usage")
            cache[str(path)] = signature, value
        values.append(cache[str(path)][1])
    return values


def summarize(root, arm, state, prefix=None):
    root = Path(root)
    stream = read(root / "task-stream.json")
    item = state["arms"][arm]
    rows = item["rows"] if prefix is None else item["rows"][:prefix]
    calls = load_receipts(root)
    calls = [c for c in calls if c["arm"] == arm]
    if prefix is not None:
        calls = [c for c in calls if step_of(c) is not None
                 and step_of(c) < (prefix if c["phase"] == "formal" else prefix - 1)]
    if len({c["id"] for c in calls}) != len(calls):
        raise RuntimeError("duplicate receipt ID")
    settled = [c for c in calls if c["state"] == "settled"]
    task = [c for c in settled if c["phase"] == "formal"]
    rsi = [c for c in settled if c["phase"] != "formal"]
    unknown = [c["id"] for c in calls if c["state"] == "requires_review"]
    pending = [c["id"] for c in calls if c["state"] == "started"]
    def tokens(cs):
        return {"input": sum(c["usage"]["prompt_tokens"] for c in cs),
                "output": sum(c["usage"]["completion_tokens"] for c in cs)}
    later = [r for r in rows if r["index"] >= 4]
    updates = [u for u in item.get("updates", []) if prefix is None or u["index"] < prefix - 1]
    phases = {name: tokens([c for c in settled if c["phase"] == name]) for name in sorted({c["phase"] for c in settled})}
    diagnostics = []
    for update in updates:
        events = update.get("events")
        changed = update.get("deployed_changed", update.get("promoted", False))
        default_status = ("deployed_skills_changed" if changed else "retained_skills") if events is not None else (
            "promoted" if changed else "retained_incumbent")
        diagnostics.append({"index": update["index"],
            "status": update.get("status", default_status),
            "proposal_count": sum(e["event"] == "proposal" for e in events) if events is not None else None,
            "candidate_evaluations": sum(e["event"] == "eval_result" for e in events) if events is not None else None,
            "native_iteration_outcomes": update.get("execution", {}).get("iterations"),
            "deployed_changed": update.get("deployed_changed", update.get("promoted", False)),
            "note_tokens": update.get("note_tokens")})
    return {"method": arm, "chain_id": stream[0]["chain_id"], "completed": len(rows),
            "passed": sum(r["reward"] for r in rows),
            "step_accuracy": sum(r["reward"] for r in rows) / len(rows) if rows else None,
            "chain_accuracy": int(all(r["reward"] == 1 for r in rows)) if len(rows) == len(stream) else None,
            "post_update_completed": len(later), "post_update_passed": sum(r["reward"] for r in later),
            "task_tokens": tokens(task), "rsi_tokens": tokens(rsi), "total_tokens": tokens(settled),
            "cost_usd": sum(c["usage"]["cost"] for c in settled),
            "unknown_usage_calls": unknown, "pending_calls": pending,
            "usage_complete": not unknown and not pending, "tokens_are_lower_bound": bool(unknown or pending),
            "updates_completed": len(updates), "update_diagnostics": diagnostics, "phase_tokens": phases,
            "deployed_changes": sum(bool(u.get("deployed_changed", u.get("promoted", False))) for u in updates),
            "status": item.get("status", state.get("state", "unknown")),
            "formal_results": [{"index": r["index"], "reward": r["reward"]} for r in rows]}


def run_report(root, state):
    arm = next(iter(state["arms"]))
    value = {"state": state["state"], "arms": [summarize(root, arm, state)],
             "equal_total_token_budget_comparison": False}
    atomic(Path(root) / "metrics.json", value)
    return value


def aggregate(session=SESSION):
    session = Path(session)
    manifest = read(session / "manifest.json")
    progress, sources = [], {}
    for item in manifest["reuse_controls"]:
        root = Path(item["root"])
        state = read(root / "native-campaign.json")
        for arm in item["arms"]:
            progress.append(summarize(root, arm, state))
            sources[(arm, item["chain"])] = (root, state)
    for item in manifest["runs"]:
        root, arm = Path(item["root"]), item["method"]
        state = read(root / "native-campaign.json") if (root / "native-campaign.json").exists() else {
            "state": "prepared_held", "arms": {arm: {"rows": [], "updates": [], "status": "prepared_held"}}}
        progress.append(summarize(root, arm, state))
        sources[(arm, item["chain"])] = (root, state)
    methods = ["kira-static", "experience-notes", "failure-only-notes", "evoskill", "meta-harness"]
    chains = [c["chain"] for c in manifest["reuse_controls"]]
    common = {c: min(len(sources[(a, c)][1]["arms"][a]["rows"]) for a in methods) for c in chains}
    matched = []
    for arm in methods:
        parts = [summarize(sources[(arm, c)][0], arm, sources[(arm, c)][1], common[c]) for c in chains]
        paired = {"wins": 0, "losses": 0, "same": 0}
        for chain, part in zip(chains, parts):
            static = sources[("kira-static", chain)][1]["arms"]["kira-static"]["rows"][:common[chain]]
            for own, control in zip(part["formal_results"], static):
                if own["index"] != control["index"]:
                    raise RuntimeError("paired task indices differ")
                paired["wins" if own["reward"] > control["reward"] else
                       "losses" if own["reward"] < control["reward"] else "same"] += 1
        count = sum(p["completed"] for p in parts)
        passed = sum(p["passed"] for p in parts)
        row = {"method": arm, "completed": count, "passed": passed,
               "step_accuracy": passed / count if count else None,
               "chain_accuracy": sum(p["chain_accuracy"] for p in parts) / len(parts)
                                 if all(p["chain_accuracy"] is not None for p in parts) else None,
               "post_update_completed": sum(p["post_update_completed"] for p in parts),
               "post_update_passed": sum(p["post_update_passed"] for p in parts),
               "deployed_changes": sum(p["deployed_changes"] for p in parts),
               "unknown_usage_calls": sum(len(p["unknown_usage_calls"]) for p in parts),
               "pending_calls": sum(len(p["pending_calls"]) for p in parts),
               "cost_usd": sum(p["cost_usd"] for p in parts)}
        row["paired_vs_static"] = paired
        row["post_update_accuracy"] = (row["post_update_passed"] / row["post_update_completed"]
                                       if row["post_update_completed"] else None)
        for name in ("task_tokens", "rsi_tokens", "total_tokens"):
            row[name] = {d: sum(p[name][d] for p in parts) for d in ("input", "output")}
        row["usage_complete"] = not row["unknown_usage_calls"] and not row["pending_calls"]
        matched.append(row)
    value = {"schema": "within-chain-five-arm-report-v1", "common_prefix_per_chain": common,
             "progress_per_chain": progress, "matched_methods": matched,
             "equal_total_token_budget_comparison": False,
             "note": "matched chronological task positions; known token usage is a lower bound when receipts are missing"}
    atomic(session / "comparison.json", value)
    buffer = io.StringIO()
    fields = ["method", "completed", "passed", "step_accuracy", "chain_accuracy", "post_update_accuracy",
              "task_tokens", "rsi_tokens", "total_tokens", "deployed_changes", "unknown_usage_calls", "cost_usd"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in matched:
        record = {k: row[k] for k in fields}
        for name in ("task_tokens", "rsi_tokens", "total_tokens"):
            record[name] = sum(record[name].values())
        writer.writerow(record)
    (session / "results.csv").write_text(buffer.getvalue())
    if all(common.values()):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
        for phase in ("task", "total"):
            fig, ax = plt.subplots(figsize=(8, 5), layout="constrained")
            for row in matched:
                ax.scatter(sum(row[phase + "_tokens"].values()) / 1e6, row["step_accuracy"] * 100,
                           label=row["method"] + (" (usage incomplete)" if not row["usage_complete"] else ""))
            ax.set(xlabel=f"{phase.title()} tokens (millions)", ylabel="Step accuracy (%)",
                   title=f"Same task positions: {common}")
            ax.grid(alpha=.2)
            ax.legend()
            fig.savefig(session / f"accuracy-vs-{phase}-tokens.png", dpi=160)
            fig.savefig(session / f"accuracy-vs-{phase}-tokens.svg")
            plt.close(fig)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, default=SESSION)
    value = aggregate(parser.parse_args().session)
    print("Matched task prefixes:", value["common_prefix_per_chain"])
