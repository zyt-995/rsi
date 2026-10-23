"""Online ModularRSI update on already-scored Evo tasks.

Uses the upstream same-bundle snapshot, trial summarizer, staged module editor,
and an independent candidate rollout. The official Phase0 TB2 task pool and
hardcoded TB2 sanity battery are deliberately replaced by past Evo tasks.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import time

from methods.comparison import DEFAULT_CONTRACT, read
from methods.evo_agents import VENDOR_AGENTS
from methods.evo_trial import run

from harbor.agents.terminus_2_modular.self_evo.k_roll import (
    extract_bundle_manifest, prepare_frozen_bundle_snapshot,
)
from harbor.agents.terminus_2_modular.self_evo.staging import prepare_staging
from harbor.agents.terminus_2_modular.self_evo.run_editor import run_editor
from harbor.agents.terminus_2_modular.self_evo.trajectory_analysis import summarize_trial

INITIAL_MODULES = VENDOR_AGENTS / "terminus_2_modular/modules"
EDITOR_SKILLS = VENDOR_AGENTS / "terminus_2_modular/self_evo/editor_skills"
LOCKED_MODULE = "tools"


def tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.name.endswith(".pyc")
            and "__pycache__" not in p.parts}


def activated_files(candidate: dict, before: dict[str, str],
                    after: dict[str, str]) -> list[str]:
    """Changed source files actually instantiated in the candidate rollout."""
    if not candidate.get("result"):
        return []
    result = read(Path(candidate["result"]))
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    active = set()
    for source in metadata.get("modular_loaded_sources", []):
        path = source["path"]
        if (source["type"] in (LOCKED_MODULE, "tool_helper")
                and source["sha256"] == after.get(path)
                and before.get(path) != after.get(path)):
            active.add(path)
    return sorted(active)


def trial_path(summary: dict) -> Path:
    return Path(summary["result"]).parent


def editor_calls(trajectory: Path | None, total: dict, uid: str) -> list[dict]:
    if trajectory is None or not trajectory.exists():
        if total.get("n_input_tokens") or total.get("n_output_tokens"):
            raise ValueError("editor_usage_without_trajectory")
        return []
    data = read(trajectory)
    rows = []
    for i, step in enumerate(data.get("steps", [])):
        metric = step.get("metrics") or {}
        if "prompt_tokens" not in metric and "completion_tokens" not in metric:
            continue
        rows.append({"call_id": f"modular-editor-{uid}-{i:03d}", "phase": "update",
                     "model": "Qwen/Qwen3.8-27B",
                     "input_tokens": int(metric.get("prompt_tokens") or 0),
                     "output_tokens": int(metric.get("completion_tokens") or 0)})
    if (sum(row["input_tokens"] for row in rows) != total.get("n_input_tokens") or
            sum(row["output_tokens"] for row in rows) != total.get("n_output_tokens")):
        raise ValueError("editor_trajectory_usage_mismatch")
    return rows


def instruction(task: str, summaries: list) -> str:
    def compact(summary) -> str:
        heading = replace(summary, trial_dir=None).to_markdown(include_reward=True)
        trace = read(Path(summary.trial_dir) / "agent/trajectory.json")
        steps = [step for step in trace.get("steps", []) if step.get("metrics")]
        if not steps:
            return heading + "\nNo model steps were recorded."
        selected = sorted(set([0, len(steps) // 2, *range(max(0, len(steps) - 3), len(steps))]))
        snapshots = []
        for i in selected:
            step = steps[i]
            commands = [str((call.get("arguments") or {}).get("keystrokes", ""))[:180]
                        for call in step.get("tool_calls") or []]
            observations = step.get("observation") or {}
            snapshots.append({"step": i + 1, "plan": str(step.get("message") or "")[:300],
                              "commands": commands[:5],
                              "observation": json.dumps(observations, ensure_ascii=False)[-450:]})
        return heading + "\nSelected early, middle, and final actions:\n" + json.dumps(snapshots, ensure_ascii=False)

    passing = next((s for s in summaries if s and s.reward == 1), None)
    failing = next((s for s in summaries if s and s.reward == 0), None)
    if failing:
        head = "Improve a reusable tools module based on this failure."
        evidence = "Failure:\n" + compact(failing)
        if passing:
            evidence += "\nSame-task passing roll:\n" + compact(passing)
    elif passing:
        head = "Improve token efficiency safely for this passed task."
        evidence = "Passing rollout:\n" + compact(passing)
    else:
        head = "Improve robustness after this unscored agent run."
        evidence = "Unscored rollout:\n" + compact(summaries[0])
    return (head + "\nUse the upstream ModularRSI module editor workflow: inspect the existing tools module, "
            "create a NEW reusable tools variant under tools/ or a solver helper under tool_helper/, "
            "validate the file, and emit <commit_patch/>. A helper may target a task family but must "
            "not depend on one task's answer or grader. "
            "Do not edit another module, installed kernel, budget, grader, or task-specific answer. "
            "The candidate will be run separately on this already-seen task. You have at most twelve turns. "
            "The selected trajectory actions below are sufficient evidence; do not read individual episode files. "
            "Read tools/baseline.py and only the reference needed for your change, then create the variant "
            "by turn four. Use create_file or edit_file actions; reasoning alone does not submit a change. "
            "Check the new file and emit <commit_patch/> by turn twelve. Keep the change small and general.\n"
            "Task: " + task + "\n" + evidence[:19000])


def public_editor_evidence(output: Path, summaries: list) -> Path:
    """Expose agent actions and reward, never grader scripts or verifier output."""
    root = output / "editor-public"
    root.mkdir(parents=True)
    for i, summary in enumerate(summaries):
        source = Path(summary.trial_dir)
        dest = root / f"roll-{i}"
        agent = dest / "agent"
        agent.mkdir(parents=True)
        trace = source / "agent/trajectory.json"
        if not trace.exists():
            raise FileNotFoundError(trace)
        shutil.copy2(trace, agent / "trajectory.json")
        pane = source / "agent/terminus_2_modular.pane"
        if pane.is_file():
            shutil.copy2(pane, agent / pane.name)
        if summary.exception_type:
            (dest / "exception.txt").write_text(summary.exception_type + "\n")
        for episode in sorted((source / "agent").glob("episode-*")):
            if episode.is_dir():
                target = agent / episode.name
                target.mkdir()
                for name in ("prompt.txt", "response.txt"):
                    file = episode / name
                    if file.is_file():
                        shutil.copy2(file, target / name)
        (dest / "score.json").write_text(json.dumps({"task": summary.task_name,
                                                      "reward": summary.reward,
                                                      "input_tokens": summary.n_input_tokens,
                                                      "output_tokens": summary.n_output_tokens}, indent=2) + "\n")
    return root


def update(index: int, formal_result: Path, modules: Path, output: Path,
           port: int = 8041, extra_rolls: int = 2,
           prior: tuple[int, Path] | None = None,
           reuse_evidence: Path | None = None) -> dict:
    contract = read(DEFAULT_CONTRACT)
    entry = contract["stream"][index]
    formal = read(formal_result)
    if formal["task_name"] != entry["task"] or formal["task_checksum"] != entry["task_checksum"]:
        raise ValueError("formal_result_task_mismatch")
    if output.exists():
        source = read(output / "source.json")
        if (source.get("formal_result") != str(formal_result)
                or source.get("modules_hashes") != tree_hashes(modules)
                or source.get("index") != index or source.get("task") != entry["task"]):
            raise ValueError("cached_update_parent_mismatch")
        decision = output / "decision.json"
        if decision.exists():
            return read(decision)
        raise FileExistsError("incomplete update exists; preserve calls and require explicit recovery")
    output.mkdir(parents=True)
    (output / "source.json").write_text(json.dumps({"formal_result": str(formal_result),
                                                     "modules": str(modules), "modules_hashes": tree_hashes(modules),
                                                     "task": entry["task"], "index": index}, indent=2) + "\n")
    formal_trials = formal_result.parent.parent
    manifest = extract_bundle_manifest(formal_trials)
    if manifest is None:
        raise ValueError("formal_bundle_missing")
    snapshot = prepare_frozen_bundle_snapshot(modules, output / "evidence", manifest)
    summaries = [summarize_trial(formal_result.parent)]
    rollout_rows = []
    for n in range(extra_rolls):
        if reuse_evidence is None:
            row = run("modularrsi", index, output / "evidence" / f"k{n+1}", port,
                      modules=snapshot.load_root, composer_name="static")
        else:
            row = read(reuse_evidence / f"k{n+1}/summary.json")
            if row["index"] != index or row["task"] != entry["task"] or not row.get("result"):
                raise ValueError("reused_rollout_task_mismatch")
        rollout_rows.append(row)
        found = extract_bundle_manifest(Path(row["result"]).parent.parent)
        if found != manifest:
            raise ValueError("extra_rollout_bundle_changed")
        summaries.append(summarize_trial(trial_path(row)))
    (output / "rollouts.json").write_text(json.dumps({"rows": rollout_rows,
                                                       "reused_from": str(reuse_evidence) if reuse_evidence else None}, indent=2) + "\n")
    if any(s is None for s in summaries):
        raise ValueError("missing_rollout_summary")
    editor_evidence = public_editor_evidence(output, summaries)
    safe_summaries = [replace(summary, trial_dir=str(editor_evidence / f"roll-{i}"))
                      for i, summary in enumerate(summaries)]
    staging = prepare_staging(modules, output / "staging")
    before = tree_hashes(staging)
    editor = asyncio.run(run_editor(staging_dir=staging,
                                    instruction=instruction(entry["task"], safe_summaries),
                                    model_name="openai/Qwen/Qwen3.8-27B",
                                    api_base=f"http://127.0.0.1:{port}/v1", api_key="EMPTY",
                                    logs_dir=output / "editor", skills_dir=EDITOR_SKILLS,
                                    trajectory_root=editor_evidence, max_turns=12,
                                    locked_module_type=LOCKED_MODULE,
                                    model_info={"max_input_tokens": 176000,
                                                "max_output_tokens": 32768,
                                                "input_cost_per_token": 0.0,
                                                "output_cost_per_token": 0.0},
                                    temperature=0.2,
                                    llm_call_kwargs={"max_tokens": 8192, "timeout": 1800,
                                                     "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}))
    calls = editor_calls(editor.trajectory_path, editor.final_metrics, entry["uid"])
    (output / "editor-calls.json").write_text(json.dumps(calls, indent=2) + "\n")
    after = tree_hashes(staging)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    allowed = bool(changed) and all(k.startswith((LOCKED_MODULE + "/", "tool_helper/"))
                                    for k in changed)
    proposal = {"success": editor.success, "committed": editor.committed,
                "n_edits": editor.n_edits, "error": editor.error, "changed_files": changed,
                "locked_module_only": allowed, "metrics": editor.final_metrics,
                "trajectory": str(editor.trajectory_path) if editor.trajectory_path else None,
                "time": time.time()}
    (output / "proposal.json").write_text(json.dumps(proposal, indent=2, default=str) + "\n")
    if not editor.success or not allowed:
        decision = {"promoted": False, "reason": "editor_failed_or_outside_locked_module", "proposal": proposal}
    else:
        candidate = run("modularrsi", index, output / "validation", port, modules=staging)
        selected = extract_bundle_manifest(output / "validation/trials")
        active_files = activated_files(candidate, before, after)
        activated = selected is not None and bool(active_files)
        baseline = ((formal.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        baseline_tokens = (formal.get("agent_result") or {}).get("n_input_tokens", 0) + (formal.get("agent_result") or {}).get("n_output_tokens", 0)
        cases = [{"index": index, "reference_reward": baseline, "candidate_reward": candidate["reward"],
                  "reference_tokens": baseline_tokens,
                  "candidate_tokens": (candidate["input_tokens"] or 0) + (candidate["output_tokens"] or 0),
                  "exception": candidate["exception"]}]
        if prior is not None:
            prior_index, prior_path = prior
            prior_result = read(prior_path)
            prior_entry = contract["stream"][prior_index]
            if prior_result["task_name"] != prior_entry["task"]:
                raise ValueError("prior_result_task_mismatch")
            previous = run("modularrsi", prior_index, output / "validation-prior", port, modules=staging)
            prior_reward = ((prior_result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            cases.append({"index": prior_index, "reference_reward": prior_reward,
                          "candidate_reward": previous["reward"],
                          "reference_tokens": (prior_result.get("agent_result") or {}).get("n_input_tokens", 0)
                                              + (prior_result.get("agent_result") or {}).get("n_output_tokens", 0),
                          "candidate_tokens": (previous["input_tokens"] or 0) + (previous["output_tokens"] or 0),
                          "exception": previous["exception"]})
        nonregression = all(case["candidate_reward"] in (0, 1)
                            and case["reference_reward"] in (0, 1)
                            and case["candidate_reward"] >= case["reference_reward"]
                            and case["exception"] is None for case in cases)
        gain = any(case["candidate_reward"] in (0, 1) and case["reference_reward"] in (0, 1)
                   and case["candidate_reward"] > case["reference_reward"] for case in cases)
        lower_tokens = sum(case["candidate_tokens"] for case in cases) <= sum(case["reference_tokens"] for case in cases)
        promoted = activated and nonregression and (gain or lower_tokens)
        decision = {"promoted": promoted,
                    "reason": "past_task_accuracy_token_pareto" if promoted else "validation_regression_dominated_or_variant_inactive",
                    "candidate": candidate, "activated": activated,
                    "activated_files": active_files,
                    "validation_cases": cases,
                    "parent_bundle": manifest.as_dict(),
                    "candidate_bundle": selected.as_dict() if selected else None,
                    "formal_reward": baseline, "proposal": proposal}
    (output / "decision.json").write_text(json.dumps(decision, indent=2, default=str) + "\n")
    return decision


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--formal-result", type=Path, required=True)
    p.add_argument("--modules", type=Path, default=INITIAL_MODULES)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, default=8041)
    p.add_argument("--extra-rolls", type=int, default=2)
    p.add_argument("--reuse-evidence", type=Path)
    a = p.parse_args()
    outcome = update(a.index, a.formal_result, a.modules, a.output, a.port, a.extra_rolls,
                     reuse_evidence=a.reuse_evidence)
    print(json.dumps({"promoted": outcome["promoted"], "reason": outcome["reason"]}, indent=2))
