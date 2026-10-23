"""Audit integration after the first formal milestone; correctness is not a gate."""
import time
from pathlib import Path

from swe_chain_evo.common import atomic, read, sha


def first_task(root, state, config):
    checks = {}
    for arm, item in state["arms"].items():
        row = item["rows"][0]
        result_path = Path(row["result"])
        result = read(result_path)
        metadata = (result.get("agent_result") or {}).get("metadata") or {}
        grade = read(row["grade"])
        directory = root / "arms" / arm / "steps" / "000000" / "formal"
        launch = read(directory / "launch.json")
        # On a transport recovery, the final launch is inside transport-retries.
        execution = launch["execution_contract"]
        receipts = [read(root / "provider-calls" / cid / "request.json")
                    for cid in dict.fromkeys(row["call_ids"])]
        settled = [r for r in receipts if r["state"] == "settled"]
        failures = []
        if execution["model"] != config["model"] or execution["solve"] != config["solve"]:
            failures.append("effective solver contract differs")
        if not row.get("usage_complete") or not settled:
            failures.append("missing authoritative usage")
        if metadata.get("agent_exception"):
            failures.append("agent execution exception")
        if metadata.get("budget_stop_reason"):
            failures.append("unexpected first-task admission/output stop")
        if row.get("exception") not in (None, "AgentTimeoutError"):
            failures.append("unexpected trial exception")
        if grade.get("missing_tests"):
            failures.append("required tests were not observed")
        patch = result_path.parent / "verifier/prediction.patch"
        if not patch.exists():
            failures.append("prediction extraction artifact missing")
        for rec in settled:
            p = rec["payload"]
            if (p.get("model") != config["model"]["id"]
                    or p.get("reasoning") != config["model"]["reasoning"]
                    or p.get("temperature") != config["model"]["temperature"]
                    or p.get("top_p") != config["model"]["top_p"]
                    or not 0 < p.get("max_tokens", 0) <= config["model"]["max_output_tokens"]
                    or p.get("provider", {}).get("only") != config["model"]["provider"]["only"]):
                failures.append("effective provider parameters differ")
                break
        for field, usage_field in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
            if row[field] != sum(rec["usage"][usage_field] for rec in settled):
                failures.append(field + " accounting mismatch")
        checks[arm] = {
            "passed": not failures, "failures": failures, "reward": row["reward"],
            "provider_calls": len(settled), "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"], "patch_bytes": patch.stat().st_size if patch.exists() else None,
            "reasoning_history_messages_restored": sum(rec.get("reasoning_history_messages_restored", 0) for rec in settled),
            "required_tests": grade.get("required_tests"),
            "observed_required_tests": grade.get("observed_required_tests"),
            "result_sha256": sha(result_path), "stop_reason": metadata.get("budget_stop_reason"),
        }
    value = {"passed": all(c["passed"] for c in checks.values()), "checked_at": time.time(),
             "accuracy_is_not_acceptance_criterion": True, "arms": checks,
             "next_gate": "audit first native RSI updates after milestone 4"}
    atomic(root / "first-task-acceptance.json", value)
    return value


def first_update(root, arm, config):
    failures = []
    receipts = [read(p) for p in (root / "provider-calls").glob("*/request.json")]
    calls = [r for r in receipts if r["arm"] == arm and r["phase"] != "formal"]
    for rec in calls:
        p = rec["payload"]
        if (p.get("model") != config["model"]["id"]
                or p.get("reasoning") != config["model"]["reasoning"]
                or p.get("top_p") != config["model"]["top_p"]
                or p.get("temperature") != config["model"]["temperature"]
                or not 0 < p.get("max_tokens", 0) <= config["model"]["max_output_tokens"]):
            failures.append("RSI provider envelope mismatch")
            break
    solves = []
    for path in (root / "arms" / arm).rglob("launch.json"):
        launch = read(path)
        if launch.get("phase") not in ("validation", "extra_rollout"):
            continue
        contract = launch.get("execution_contract", {})
        if contract.get("model") != config["model"] or contract.get("solve") != config["solve"]:
            failures.append("RSI task solver envelope mismatch")
        solves.append({"path": str(path), "task_index": launch["index"], "phase": launch["phase"]})
    value = {"passed": not failures, "arm": arm, "failures": failures,
             "checked_at": time.time(), "calls": len(calls), "validation_solves": solves,
             "usage_complete": not any(r["state"] in ("started", "requires_review") for r in calls),
             "candidate_improvement_is_not_acceptance_criterion": True}
    atomic(root / ("first-update-acceptance-" + arm + ".json"), value)
    return value
