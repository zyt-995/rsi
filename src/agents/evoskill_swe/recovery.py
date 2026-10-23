"""Audited recovery of pre-dispatch skill failures and paid native mutations."""
from pathlib import Path

from swe_chain_evo.common import atomic, read, sha
from .skills import hashes

LEGACY_EMPTY_AGENT = "d3da597146a3165e5bd06be8e69d4b420842375c29b7e60ad2a58d261adb427e"
LEGACY_EMPTY_TRIAL = "82043ffc8a27e9cab9b705f6af307c6ec8e359dde60c4107a9a73359ad7bc45c"


def compatible_contract(saved, current, identity):
    if saved == current:
        return True
    if identity["source"].get("skills_hashes"):
        return False
    # Empty skills never entered the broken inherited discovery path. Preserve
    # the actual old contract in returned paid summaries, without relabeling it.
    legacy = {**current, "evoskill_agent_sha256": LEGACY_EMPTY_AGENT,
              "evoskill_adapter_sha256": LEGACY_EMPTY_TRIAL}
    return saved == legacy


def predispatch_skill_failure(output, row):
    if (row.get("exception") != "TypeError" or row.get("call_ids") != []
            or not row.get("usage_complete") or row.get("cost_usd") != 0
            or row.get("input_tokens") != 0 or row.get("output_tokens") != 0):
        return False
    if sha(Path(row["result"])) != row["result_sha256"] or sha(Path(row["grade"])) != row["grade_sha256"]:
        raise ValueError("immutable failed infrastructure trial changed")
    result = Path(row["result"])
    error_file = result.parent / "agent/agent-exception.json"
    if not error_file.exists():
        return False
    error = read(error_file)
    trace = error.get("traceback", "")
    return (error.get("type") == "TypeError" and "PosixPath" in trace
            and "_build_skills_section" in trace and "shlex.quote" in trace
            and bool(read(result).get("finished_at")))


def attach_pending_mutation(loop, manager, workspace, output):
    checkpoint = Path(output) / "pending-mutation-resume.json"
    if not checkpoint.exists():
        return
    saved = read(checkpoint)
    child, proposal, justification = saved["mutation_result"]
    if manager.get_frontier() != saved["frontier"]:
        raise RuntimeError("pending mutation frontier changed")
    from .native import git
    if git(workspace, "rev-parse", "program/" + child) != saved["child_commit"]:
        raise RuntimeError("paid candidate branch changed")
    for evidence in saved["evidence"]:
        if sha(Path(evidence["path"])) != evidence["sha256"]:
            raise RuntimeError("paid native mutation evidence changed")
    # Resume the unfinished iteration, not the next chronological iteration.
    loop._get_highest_iteration = lambda: saved["iteration"] - 1

    async def restored(parent, failures, iteration):
        if (parent, iteration) != (saved["parent"], saved["iteration"]):
            raise RuntimeError("pending native mutation parent/iteration changed")
        manager.switch_to(child)
        if hashes(Path(workspace) / ".claude/skills") != saved["skills_hashes"]:
            raise RuntimeError("paid candidate skills changed")
        loop._iter_cost += saved["meta_cost_usd"]
        for event in saved["mutation_events"]:
            loop._emit(event["event"], **event["data"])
        atomic(Path(output) / "native-mutation-recovery.json", {
            "checkpoint_sha256": sha(checkpoint), "child": child,
            "paid_proposer_replayed": False, "paid_generator_replayed": False})
        return child, proposal, justification

    loop._mutate_with_fallback = restored
