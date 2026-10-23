"""Call official EvoSkill loop, proposer, generator and frontier without rewriting them."""
import asyncio
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from unittest.mock import patch

from . import RELEASE, VENDOR
from .skills import hashes, require_inventory, normalize_generated, verify_discovery
from .recovery import attach_pending_mutation
from .traces import HarborTaskTrace
from .execution import audit_execution
from swe_chain_evo.common import atomic, read, sha
from src.harness import Agent, AgentTrace, set_sdk
from src.schemas import AgentResponse, SkillProposerResponse, ToolGeneratorResponse
from src.loop.config import LoopConfig
from src.loop.runner import SelfImprovingLoop, LoopAgents
from src.registry import ProgramManager
from src.evaluation.evaluate import EvalResult
from src.evaluation.harbor_scorer import harbor_reward_scorer
from methods.evo_fifty.trial import NetworkUnavailable


def serial(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=serial).encode()).hexdigest()


class TaskAgent(Agent):
    """The official Harbor inner-agent interface, using the existing scored transport."""
    def __init__(self, runner, index, workspace, output):
        super().__init__(options={"sdk": "claude", "model": runner.config["model"]["id"]},
                         response_model=AgentResponse, max_retries=1)
        self.runner, self.index, self.workspace, self.output = runner, index, workspace, output
        self.allowed = {e["task"]: i for i, e in enumerate(runner.stream) if i <= index}
        self.phase = "extra_rollout"
        self.counter = 0
        self.traces = []

    async def run(self, query):
        if query not in self.allowed:
            raise ValueError("EvoSkill requested an uncompleted/future task")
        require_inventory(self.workspace / ".claude/skills")
        self.counter += 1
        sequence = self.counter
        # Separate every official evaluation/sample call; only exact resumed calls are cached.
        skills = self.output / "rollout-skills" / f"{sequence:04d}"
        source = self.workspace / ".claude/skills"
        current = hashes(source)
        if skills.exists():
            if hashes(skills) != current:
                raise RuntimeError("interrupted native rollout skill state changed")
        else:
            shutil.copytree(source, skills)
        trial = self.output / "rollouts" / f"{sequence:04d}-{self.phase}"
        started = time.monotonic()
        while True:
            try:
                row = await asyncio.to_thread(self.runner.run, "evoskill", self.allowed[query], trial,
                                              phase=self.phase, skills=skills)
                break
            except NetworkUnavailable:
                await asyncio.sleep(self.runner.config["runtime"]["network_recovery_cooldown_seconds"])
        if row.get("exception"):
            raise RuntimeError("internal task execution exception; exclude infrastructure failure from native score")
        result = Path(row["result"])
        discovery = verify_discovery(result, skills)
        trajectory = result.parent / "agent/trajectory.json"
        public = {"task": self.runner.stream[self.allowed[query]]["instruction"],
                  "reward": row["reward"], "agent_trajectory": read(trajectory) if trajectory.exists() else None}
        envelope = {"reward": row["reward"], "task_id": query, "exit_status": "verified"}
        cached_trace = trial / "native-trace.json"
        trace = HarborTaskTrace.model_validate(read(cached_trace)) if cached_trace.exists() else HarborTaskTrace(uuid=trial.name, session_id=trial.name,
            model=self.runner.config["model"]["id"], tools=[],
            duration_ms=int(1000 * (time.monotonic() - started)), total_cost_usd=row["cost_usd"],
            num_turns=len(row["call_ids"]), usage={"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]},
            result=json.dumps(public, ensure_ascii=False), is_error=False,
            output=AgentResponse(final_answer=json.dumps(envelope), reasoning="official benchmark verifier reward"),
            messages=[])
        atomic(cached_trace, trace.model_dump())
        self.traces.append({"sequence": sequence, "task": query, "phase": self.phase,
                            "result_sha256": row["result_sha256"], "call_ids": row["call_ids"],
                            "skill_discovery": discovery})
        return trace


async def evaluate_with_common_timeout(agent, items, max_concurrent=2, *, cache=None):
    """Evaluator seam: keep native scores, use the shared task timeout and transport recovery."""
    if cache is not None:
        raise ValueError("cross-validation-window run cache must be disabled")
    semaphore = asyncio.Semaphore(max_concurrent)
    previous = agent.phase
    agent.phase = "validation"
    async def one(question, ground_truth):
        async with semaphore:
            trace = await agent.run(question)
            return EvalResult(question, ground_truth, trace)
    try:
        return await asyncio.gather(*(one(q, a) for q, a in items))
    finally:
        agent.phase = previous


class MetaAgent(Agent):
    """Official Agent/Claude SDK execution, with scoped billing and workspace isolation."""
    def __init__(self, runner, workspace, output, role, options, response_model):
        super().__init__(options=options, response_model=response_model,
                         timeout_seconds=runner.config["arms"]["evoskill"]["meta_timeout_seconds"], max_retries=1)
        self.runner, self.workspace, self.output, self.role = runner, workspace, output, role

    async def _execute_query(self, query):
        if getattr(self, "_paid_context_resuming", False):
            from .context_recovery import execute_resumed_query
            return await execute_resumed_query(self._get_options(), query)
        return await super()._execute_query(query)

    async def run(self, query):
        config = self.runner.config
        identity = {"role": self.role, "query": query,
                    "skills": hashes(self.workspace / ".claude/skills"),
                    "model": config["model"], "official_commit": config["official_method"]["commit"]}
        target = self.output / "meta" / self.role / digest(identity)
        from .context_recovery import checked_resume, existing_scope, configure_context
        resume = checked_resume(self.runner, self.output, target, identity, self.role)
        original_target = target
        cached = target / "context-resume/trace.json" if resume else target / "trace.json"
        if cached.exists():
            if read(target / "identity.json") != identity:
                raise RuntimeError("native meta checkpoint changed")
            require_inventory(self.workspace / ".claude/skills")
            return AgentTrace[self.response_model].model_validate(read(cached))
        if (target / "started.json").exists() and not resume:
            raise RuntimeError("partial paid native meta session retained; inspect before replay")
        if resume and (target / "context-resume/started.json").exists():
            raise RuntimeError("partial exact paid continuation retained; inspect before another continuation")
        token = existing_scope(self.runner, resume, target) if resume else self.runner.broker.scope(
            "evoskill", self.role, str(target.relative_to(self.runner.root)))
        model = config["model"]["id"]
        options = self._get_options()
        options.cli_path = str(RELEASE / "scripts/claude_isolated.py")
        options.model = model
        options.max_turns = config["arms"]["evoskill"]["max_meta_turns"]
        options.permission_mode = "bypassPermissions"
        options.setting_sources = ["project"]
        options.max_buffer_size = 20_000_000
        # Restrict actual tools to exactly the original profile list.
        options.tools = list(options.allowed_tools)
        options.env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.runner.port}",
            "ANTHROPIC_API_KEY": token, "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": model, "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CONFIG_DIR": str(self.workspace / ".evoskill/claude-private"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_UPDATE": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(config["model"]["max_output_tokens"]),
            "EVOSKILL_META_WORKSPACE": str(self.workspace), "EVOSKILL_META_ROLE": self.role,
            "NO_PROXY": "127.0.0.1,localhost"}
        for role in ("OPUS", "SONNET", "HAIKU", "FABLE"):
            options.env["ANTHROPIC_DEFAULT_" + role + "_MODEL"] = model
        remaining_seconds = configure_context(options, config, resume)
        self._paid_context_resuming = bool(resume)
        if resume:
            target = target / "context-resume"
            query = "Continue the interrupted skill proposal in this same session and finish the configured structured response."
            cached = target / "trace.json"
        # Official executor's preflight requires this variable. The CLI receives the scoped local token above.
        os.environ["ANTHROPIC_API_KEY"] = "local-scoped-gateway-only"
        os.environ.pop("CLAUDECODE", None)
        target.mkdir(parents=True, exist_ok=True)
        atomic(target / "identity.json", identity)
        atomic(target / "started.json", {"time": time.time(), "role": self.role})
        (target / "query.txt").write_text(query)
        with (target / "stderr.log").open("a") as log:
            options.stderr = lambda message: (log.write(message + "\n"), log.flush())
            self._options = options
            prior_timeout = self.timeout_seconds
            if remaining_seconds is not None:
                self.timeout_seconds = remaining_seconds
            try:
                trace = await super().run(query)
            finally:
                self.timeout_seconds = prior_timeout
        # SDK model prices assume Claude. Provider receipts are authoritative.
        scope = self.runner.broker.scopes[token]
        calls = [read(self.runner.root / "provider-calls" / cid / "request.json") for cid in scope["calls"]]
        if scope["blocked"] or scope["pending_tokens"]:
            raise RuntimeError("native meta accounting or transport requires review")
        usages = [c["usage"] for c in calls if c["state"] == "settled"]
        trace.total_cost_usd = sum(u["cost"] for u in usages)
        trace.model = model
        trace.usage = {"input_tokens": sum(u["prompt_tokens"] for u in usages),
                       "output_tokens": sum(u["completion_tokens"] for u in usages)}
        if self.role == "skill_generator":
            validity = normalize_generated(self.workspace / ".claude/skills", query)
            atomic(target / "skill-validity.json", validity)
            if trace.output is not None and not validity["skills"]:
                raise ValueError("skill generator returned success without a discoverable task skill")
        # Preserve the full official SDK transcript and structured output.
        atomic(target / "trace.json", json.loads(json.dumps(trace.model_dump(), default=serial)))
        atomic(target / "provider-accounting.json", {"call_ids": scope["calls"],
            "usage_complete": not any(c["state"] == "requires_review" for c in calls),
            "official_sdk_cost_ignored": True})
        return trace


def git(workspace, *args):
    return subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True, text=True).stdout.strip()


def prepare_workspace(output, previous=None, initial_skills=None):
    output = Path(output)
    workspace = output / "workspace"
    if workspace.exists():
        # Same-update continuation must retain its own exact resume marker.
        return workspace
    if previous:
        # Copy learned programs/frontier/feedback, but a new support window is
        # not an exact resume of the previous loop. Its completed-iteration
        # marker can otherwise suppress this window's only native iteration.
        staging = output / "workspace-copying"
        if staging.exists():
            shutil.rmtree(staging)  # No model runs use this unpublished copy.
        shutil.copytree(previous, staging, ignore=shutil.ignore_patterns("claude-private", "__pycache__"))
        marker = staging / ".evoskill/loop_checkpoint.json"
        if marker.exists():
            archived = output / "inherited-loop-checkpoint.json"
            if archived.exists() and archived.read_bytes() != marker.read_bytes():
                raise RuntimeError("inherited loop checkpoint archive changed")
            shutil.copyfile(marker, archived)
            marker.unlink()
        staging.rename(workspace)
    else:
        workspace.mkdir(parents=True)
        (workspace / ".claude/skills").mkdir(parents=True)
        if initial_skills is not None:
            # Cross-chain state consists only of the selected immutable task skills.
            # Start a new registry; do not import Click scores, sessions or credentials.
            shutil.copytree(initial_skills, workspace / ".claude/skills", dirs_exist_ok=True)
        (workspace / ".evoskill").mkdir()
        (workspace / ".gitignore").write_text(".evoskill/\npublic-history/\n")
        git(workspace, "init", "-b", "main")
        git(workspace, "config", "user.name", "RSI Experiment")
        git(workspace, "config", "user.email", "rsi-experiment@localhost")
        git(workspace, "add", ".gitignore", ".claude/skills")
        git(workspace, "commit", "-m", "Initialize empty task skills")
    # Empty mount points contain no seeded task skills. Sandbox overlays provide the helpers.
    for name in ("brainstorming", "skill-creator"):
        (workspace / ".claude/skills" / name).mkdir(parents=True, exist_ok=True)
    return workspace


async def update(runner, index, previous, output):
    """One native iteration on completed tasks; all learned branches are retained."""
    output = Path(output)
    saved = output / "outcome.json"
    if saved.exists():
        result = read(saved)
        if not result.get("execution", {}).get("passed"):
            raise RuntimeError("cached update lacks execution evidence; preserve and review the original outcome")
        if hashes(result["skills"]) != result["skills_hashes"]:
            raise RuntimeError("deployed skill snapshot changed")
        require_inventory(result["skills"])
        return result
    set_sdk("claude")
    cfg = runner.config["arms"]["evoskill"]
    workspace = prepare_workspace(output, Path(previous) if previous else None, runner.root / "initial/skills")
    support = runner.stream[max(0, index - cfg["validation_task_limit"] + 1):index + 1]
    support_ids = [e["task"] for e in support]
    if len(support) != 2:
        raise ValueError("expected the same two completed support tasks as other arms")
    categories = {e.get("repo", "repository") for e in support}
    if len(categories) != 1:
        raise ValueError("native support window spans different repositories")
    category = categories.pop()
    public = workspace / "public-history"
    public.mkdir(exist_ok=True)
    atomic(public / "completed.json", [{"task_id": e["task"], "instruction": e["instruction"]} for e in support])
    base = TaskAgent(runner, index, workspace, output)
    from src.agent_profiles.skill_proposer.skill_proposer import make_skill_proposer_options
    from src.agent_profiles.skill_generator.skill_generator import make_skill_generator_options
    model = runner.config["model"]["id"]
    proposer = MetaAgent(runner, workspace, output, "skill_proposer",
        make_skill_proposer_options(project_root=str(workspace), model=model), SkillProposerResponse)
    generator = MetaAgent(runner, workspace, output, "skill_generator",
        make_skill_generator_options(project_root=str(workspace), model=model), ToolGeneratorResponse)
    agents = LoopAgents(base=base, skill_proposer=proposer, skill_generator=generator,
                        prompt_proposer=None, prompt_generator=None)
    manager = ProgramManager(workspace)
    loop_config = LoopConfig(max_iterations=cfg["max_iterations"], frontier_size=cfg["frontier_size"],
        no_improvement_limit=cfg["no_improvement_limit"], concurrency=runner.config["runtime"]["validation_concurrency"],
        evolution_mode="skill_only", selection_strategy=cfg["selection_strategy"],
        categories_per_batch=cfg["categories_per_batch"], samples_per_category=cfg["samples_per_category"],
        failure_sample_count=cfg["failure_sample_count"], continue_mode=bool(manager.get_frontier()),
        reset_feedback=False, cache_enabled=False)
    pending = output / "pending-mutation-resume.json"
    from .context_recovery import context_manifest
    context_resume = context_manifest(output)
    if context_resume:
        # Resume the same unfinished iteration and reuse its exact four scored
        # rollout checkpoints, rather than opening a new validation window.
        loop_config.continue_mode = context_resume["original_continue_mode"]
        events = [e for e in read(output / "events.json") if e["event"] != "loop_done"]
    else:
        events = read(output / "events.json") if pending.exists() else []
    replay_events = list(events)
    replay_cursor = 0
    def event(name, data):
        nonlocal replay_cursor
        if replay_cursor < len(replay_events):
            expected = replay_events[replay_cursor]
            if (name, data) != (expected["event"], expected["data"]):
                raise RuntimeError("native resume event prefix changed")
            replay_cursor += 1
            return
        events.append({"event": name, "data": data, "time": time.time()})
        atomic(output / "events.json", events)
    if pending.exists():
        loop_config.continue_mode = read(pending)["loop_continue_mode"]
    loop = SelfImprovingLoop(loop_config, agents, manager,
        train_pools={category: [(e["task"], "1.0") for e in support]},
        val_data=[(e["task"], "1.0", category) for e in support], scorer=harbor_reward_scorer,
        on_event=event,
        task_constraints=("These are chronological SWE-Chain-Evo coding milestones. Task IDs in traces refer to the public "
            "instructions shown in public-history/completed.json. Ground truth 1.0 is only the desired verifier reward, "
            "not a reference patch. Learn reusable coding skills using only these past public execution traces. "
            "Future milestones, hidden tests, reference patches and coordinator files are unavailable. "
            "Create or edit task skills only; brainstorming and skill-creator are fixed authoring helpers."))
    atomic(output / "native-entry.json", {"repository_commit": runner.config["official_method"]["commit"],
        "official_loop": str(VENDOR / "src/loop/runner.py"), "loop_sha256": sha(VENDOR / "src/loop/runner.py"),
        "official_frontier": str(VENDOR / "src/registry/manager.py"),
        "support_indices": list(range(index - 1, index + 1)), "support_tasks": support_ids,
        "support_category": category,
        "loop_config": asdict(loop_config), "adaptations": runner.config["adaptations"]})
    attach_pending_mutation(loop, manager, workspace, output)
    if context_resume:
        loop._get_highest_iteration = lambda: context_resume["iteration"] - 1
    # A new chronological validation window invalidates previous numeric rankings.
    # Re-evaluate every native frontier member; retain official ranking/tie rules.
    refreshed = []
    with patch("src.loop.runner.evaluate_agent_parallel", evaluate_with_common_timeout):
        frontier = manager.get_frontier()
        if frontier and not context_resume and (not pending.exists() or read(pending)["refresh_frontier"]):
            for name in frontier:
                manager.unmark_frontier(name)
            for name in frontier:
                manager.switch_to(name)
                score = await loop._evaluate(loop.val_data)
                manager.update_frontier(name, score, max_size=cfg["frontier_size"])
                refreshed.append({"program": name, "score": score})
            atomic(output / "frontier-refresh.json", refreshed)
        result = await loop.run()
    execution = audit_execution(events, loop_config, support_ids)
    execution["events_sha256"] = sha(output / "events.json")
    atomic(output / "execution-check.json", execution)
    if not execution["passed"]:
        raise RuntimeError("native update did not execute as configured: " + "; ".join(execution["errors"]))
    manager.switch_to(result.best_program)
    selected_inventory = require_inventory(workspace / ".claude/skills")
    deployed = output / (context_resume.get("deployed_skills_directory", "deployed-skills-context-resumed") if context_resume else "deployed-skills")
    shutil.copytree(workspace / ".claude/skills", deployed)
    if require_inventory(deployed) != selected_inventory:
        raise RuntimeError("deployed skill inventory differs from selected program")
    atomic(output / "deployment-inventory.json", {"skills": selected_inventory, "passed": True})
    value = {"index": index, "workspace": str(workspace), "skills": str(deployed),
        "skills_hashes": hashes(deployed), "best_program": result.best_program, "frontier": result.frontier,
        "iterations_completed": result.iterations_completed, "refreshed_frontier": refreshed,
        "selected_commit": git(workspace, "rev-parse", "HEAD"), "support_tasks": support_ids,
        "events": events, "execution": execution, "rollouts": base.traces, "finished": time.time()}
    atomic(saved, value)
    return value
