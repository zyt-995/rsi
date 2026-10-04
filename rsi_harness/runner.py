from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .budget import BudgetExceeded, CostTracker
from .metrics import summarize, write_task_csv
from .program import Candidate, DockerProgramExecutor, ProgramExecutor, ProgramStore
from .state import StateStore, write_json
from .types import MODES, Action, AgentBackend, BenchmarkAdapter


SELF_SCHEMAS = {
    "WRITE_ARTIFACT": {"description": "Add or replace a persistent artifact; effective on next decision",
                       "payload": {"path": "MEMORY.md | AGENTS.md | skills/... | scripts/...", "content": "string"}},
    "DELETE_ARTIFACT": {"description": "Delete an artifact", "payload": {"path": "string"}},
    "ROLLBACK": {"description": "Restore a previous artifact revision (does not rewind environment or budget)",
                 "payload": {"revision": "integer"}},
}

PROGRAM_SCHEMAS = {
    "PROPOSE_AGENT": {
        "description": "Propose an executable agent version. It is sandboxed and must pass validation before deployment.",
        "payload": {"label": "short description", "files": {
            "agent.py": "required Python entrypoint; files replaces the complete active program",
            "workflow.py": "optional supporting source",
            "prompts/policy.md": "optional prompt or data file",
        }},
        "output_contract": {
            "direct": {"action": {"type": "an available action", "payload": {}, "reason": "string"}},
            "delegate": {"delegate": {"instructions": "extra policy instructions", "context": {}}},
        },
    },
    "ROLLBACK_AGENT": {
        "description": "Deploy the contents of a past executable-agent revision as a new revision.",
        "payload": {"revision": "integer"},
    },
}


@dataclass(frozen=True)
class RunConfig:
    mode: str = "static"
    budget: int = 200
    self_budget: int = 60
    max_steps: int = 12
    improvement_steps: int = 2
    history_bytes: int = 32768
    state_bytes: int = 65536
    decision_cost: int = 1
    task_action_cost: int = 1
    self_action_cost: int = 1
    program_run_cost: int = 1
    validation_cases: int = 3
    evolution_candidates: int = 3
    program_bytes: int = 131072
    program_image: str = "python:3.10-slim"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        for key in ("budget", "self_budget", "history_bytes"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        for key in ("max_steps", "improvement_steps", "state_bytes", "decision_cost", "task_action_cost",
                    "self_action_cost", "program_run_cost", "evolution_candidates", "program_bytes"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if type(self.validation_cases) is not int or self.validation_cases < 0:
            raise ValueError("validation_cases must be a nonnegative integer")


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self.sequence = 0

    def emit(self, event: str, **payload) -> None:
        record = {"sequence": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
                  "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
        self.sequence += 1


class Runner:
    def __init__(self, benchmark: BenchmarkAdapter, backend: AgentBackend, config: RunConfig, output: Path,
                 program_executor: ProgramExecutor | None = None):
        self.benchmark, self.backend, self.config = benchmark, backend, config
        self.output = Path(output)
        # A new run never overwrites or silently resumes an old experiment.
        self.output.mkdir(parents=True, exist_ok=False)
        self.state = StateStore(self.output / "state", config.state_bytes)
        self.program = None
        self.program_executor = None
        if config.mode in {"open-ended", "evolution"}:
            self.program = ProgramStore(self.output / "program", config.program_bytes)
            self.program_executor = program_executor or DockerProgramExecutor(config.program_image)
            bind = getattr(self.program_executor, "bind", None)
            if bind:
                bind(self.program.root)
        self.cost = CostTracker(config.budget, config.self_budget)
        self.log = EventLog(self.output / "events.jsonl")
        self.history: list[dict] = []
        self.completed_task_ids: list[str] = []

    def run(self) -> dict:
        code_hash = hashlib.sha256()
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            code_hash.update(str(path.relative_to(Path(__file__).parent)).encode())
            code_hash.update(path.read_bytes())
        write_json(self.output / "manifest.json", {
            "schema_version": 1, "package_version": __version__, "python": platform.python_version(),
            "source_sha256": code_hash.hexdigest(), "config": asdict(self.config),
            "benchmark": self.benchmark.manifest(), "backend": self.backend.manifest(),
            "program_executor": self.program_executor.manifest() if self.program_executor else None,
            "cost_semantics": "fixed decision/action units; provider tokens reported separately",
        })
        rows = []
        stopped = None
        self.log.emit("run_started")
        try:
            for task in self.benchmark.tasks():
                if stopped:
                    rows.append({"task_id": task.id, "evaluation": task.evaluation,
                                 "attempted": False, "completed": False, "reward": 0.0,
                                 "status": "unattempted_" + stopped, "task_units": 0, "self_units": 0,
                                 "state_changes": 0, "invalid_actions": 0, "revision": self.state.revision})
                    continue
                row, public_history = self._task(task)
                rows.append(row)
                if row["completed"]:
                    self.completed_task_ids.append(task.id)
                if self.config.mode == "context-only":
                    self.history.append(public_history)
                    while self.history and len(json.dumps(self.history, ensure_ascii=False).encode()) > self.config.history_bytes:
                        self.history.pop(0)
                if row["status"] in {"budget_exhausted", "backend_error"}:
                    stopped = row["status"]
        except Exception as exc:
            self.log.emit("run_failed", error_type=type(exc).__name__)
            write_json(self.output / "failure.json", {"error_type": type(exc).__name__, "cost": self.cost.view()})
            self._close_program_executor()
            raise
        summary = summarize(rows, self.cost.view())
        summary.update({"mode": self.config.mode, "benchmark": self.benchmark.name,
                        "status": stopped or "finished", "final_revision": self.state.revision,
                        "final_program_revision": self.program.revision if self.program else None})
        write_json(self.output / "tasks.json", rows)
        write_task_csv(self.output / "tasks.csv", rows)
        write_json(self.output / "summary.json", summary)
        self.log.emit("run_finished", summary=summary)
        self._close_program_executor()
        return summary

    def _close_program_executor(self) -> None:
        close = getattr(self.program_executor, "close", None)
        if close:
            close()

    def _request(self, task_id, phase, observation, trajectory, feedback,
                 phase_step: int = 0) -> dict:
        schemas = dict(self.benchmark.actions()) if phase == "act" else {"NOOP": {"payload": {}}}
        artifact_phase = (
            (self.config.mode == "fixed-loop" and phase == "improve") or
            (self.config.mode == "self-action" and phase in {"act", "reflect"})
        )
        if artifact_phase:
            if self.cost.self_limit - self.cost.self_units >= self.config.self_action_cost:
                schemas.update(SELF_SCHEMAS)
        if self.program and phase in {"improve", "search"}:
            if self.cost.self_limit - self.cost.self_units >= self.config.self_action_cost:
                schemas["PROPOSE_AGENT"] = PROGRAM_SCHEMAS["PROPOSE_AGENT"]
                # Evolution compares a same-parent population. A rollback would
                # change the parent half-way through that population.
                if self.config.mode == "open-ended":
                    schemas["ROLLBACK_AGENT"] = PROGRAM_SCHEMAS["ROLLBACK_AGENT"]
        program_view = None
        if self.program:
            program_view = self.program.view(include_files=phase in {"improve", "search"})
        guidance = {
            "act": ("Solve the current task. In self-action mode you may spend a step on a persistent "
                    "artifact change when that is more valuable than an immediate task action."),
            "improve": ("The task has ended. Extract a reusable, non-task-specific improvement from the "
                        "trajectory and terminal feedback, or choose NOOP."),
            "reflect": ("The task has ended. Decide autonomously whether terminal evidence justifies a "
                        "persistent artifact change, or choose NOOP."),
            "search": ("Propose one distinct complete executable-agent candidate for this population slot. "
                       "Use the visible active source; do not encode hidden answers or future-task assumptions."),
            "validation": "Solve the replay case without modifying persistent state.",
        }[phase]
        return {"schema_version": 1, "task_id": task_id, "phase": phase,
                "mode": self.config.mode, "phase_guidance": guidance,
                "phase_step": phase_step,
                "search": ({"slot": phase_step, "population_size": self.config.evolution_candidates,
                            "parent_revision": self.program.revision}
                           if phase == "search" else None),
                "observation": observation, "trajectory": trajectory, "feedback": feedback,
                "state": self.state.view(), "history": self.history if self.config.mode == "context-only" else [],
                "agent_program": program_view,
                "actions": schemas, "budget": self.cost.view(),
                "prices": {"decision": self.config.decision_cost, "task_action": self.config.task_action_cost,
                           "self_action": self.config.self_action_cost,
                           "program_run": self.config.program_run_cost}}

    def _backend_decide(self, request: dict, category: str, event: str = "decision") -> Action:
        self.cost.charge(self.config.decision_cost, category)
        self.cost.decisions += 1
        self.cost.backend_calls += 1
        request["budget"] = self.cost.view()
        # JSON roundtrip isolates trusted runner objects from an in-process backend.
        public_request = json.loads(json.dumps(request, ensure_ascii=False))
        self.log.emit(event + "_started", task_id=request["task_id"], request=public_request)
        start = time.monotonic()
        try:
            response = self.backend.decide(public_request)
            if not isinstance(response, dict):
                raise ValueError("backend response must be an object")
            self.cost.record_usage(response.get("usage", {}))
            action = Action.from_dict(response.get("action"))
            self.log.emit(event + "_finished", task_id=request["task_id"], response=response)
            return action
        finally:
            self.cost.backend_seconds += time.monotonic() - start

    def _decide(self, request: dict) -> Action:
        category = "self" if request["phase"] in {"improve", "reflect", "search", "validation"} else "task"
        if not self.program:
            return self._backend_decide(request, category)
        # Program execution is part of this policy decision. A delegating program
        # consumes one model decision; a direct program action consumes the same
        # fixed decision unit but no provider tokens.
        output = self._run_program(self.program.active_directory(), request, category)
        if "action" in output:
            self.cost.charge(self.config.decision_cost, category)
            self.cost.decisions += 1
            self.log.emit("program_decision", task_id=request["task_id"], program=self.program.view(),
                          action=output["action"], cost=self.cost.view())
            return Action.from_dict(output["action"])
        delegated = json.loads(json.dumps(request, ensure_ascii=False))
        delegated["program_instructions"] = output["delegate"]["instructions"]
        delegated["program_context"] = output["delegate"]["context"]
        return self._backend_decide(delegated, category)

    def _run_program(self, directory: Path, request: dict, category: str) -> dict:
        self.cost.charge(self.config.program_run_cost, category)
        self.cost.program_runs += 1
        started = time.monotonic()
        try:
            return self.program_executor.run(directory, request)
        finally:
            self.cost.program_seconds += time.monotonic() - started

    def _validation_suite(self, current_task_id: str | None = None) -> list[dict]:
        method = getattr(self.benchmark, "validation_cases", None)
        task_ids = list(self.completed_task_ids)
        if current_task_id and current_task_id not in task_ids:
            task_ids.append(current_task_id)
        if not method or not task_ids or self.config.validation_cases == 0:
            return []
        cases = method(task_ids, self.config.validation_cases)
        if not isinstance(cases, list):
            raise ValueError("validation_cases must return a list")
        return cases

    def _score_program(self, directory: Path, suite: list[dict], smoke_request: dict) -> dict:
        # The smoke case always checks the executable contract without exposing
        # any evaluator metadata to generated code.
        self._run_program(directory, smoke_request, "self")
        if not suite:
            return {"passed": False, "kind": "no-behavior-validation", "cases": 0,
                    "score": None, "model_calls": 0}
        scorer = getattr(self.benchmark, "score_validation", None)
        if not scorer:
            raise ValueError("benchmark supplies validation cases without score_validation")
        scores = []
        model_calls = 0
        for case in suite:
            if not isinstance(case, dict) or not isinstance(case.get("request"), dict):
                raise ValueError("invalid private validation case")
            request = json.loads(json.dumps(case["request"], ensure_ascii=False))
            request["mode"] = self.config.mode
            request["phase_guidance"] = "Solve the replay case without modifying persistent state."
            request.setdefault("state", self.state.view())
            request.setdefault("history", [])
            # Validation is act-like. Source is visible only to the improver/search
            # phase, not to either policy while it is being scored.
            request.setdefault("agent_program", self.program.view(include_files=False))
            request.setdefault("budget", self.cost.view())
            request.setdefault("prices", {"decision": self.config.decision_cost,
                                           "task_action": self.config.task_action_cost,
                                           "self_action": self.config.self_action_cost,
                                           "program_run": self.config.program_run_cost})
            # Temporarily execute the requested directory without deploying it.
            output = self._run_program(directory, request, "self")
            if "action" in output:
                self.cost.charge(self.config.decision_cost, "self")
                self.cost.decisions += 1
                action = Action.from_dict(output["action"])
            else:
                model_calls += 1
                delegated = json.loads(json.dumps(request, ensure_ascii=False))
                delegated["phase"] = "validation"
                delegated["program_instructions"] = output["delegate"]["instructions"]
                delegated["program_context"] = output["delegate"]["context"]
                action = self._backend_decide(delegated, "self", "validation_decision")
            if action.type not in request.get("actions", {}):
                scores.append(0.0)
            else:
                score = scorer(case, action)
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    raise ValueError("validation score must be numeric")
                scores.append(float(score))
        return {"passed": True, "kind": "task-replay", "cases": len(scores),
                "score": sum(scores) / len(scores), "scores": scores,
                "model_calls": model_calls}

    @staticmethod
    def _beats(candidate: dict, active: dict) -> bool:
        if candidate.get("score") is None or active.get("score") is None:
            return False
        return (
            candidate["score"] > active["score"] or
            (candidate["score"] > 0 and candidate["score"] == active["score"] and
             candidate.get("model_calls", 0) < active.get("model_calls", 0))
        )

    @staticmethod
    def _public_validation(result: dict) -> dict:
        return {key: result.get(key) for key in ("passed", "kind", "cases")}

    def _validate_candidate(self, candidate: Candidate, smoke_request: dict,
                            current_task_id: str, active_cache: list[dict | None]) -> dict:
        try:
            suite = self._validation_suite(current_task_id)
            candidate_result = self._score_program(candidate.directory, suite, smoke_request)
            if active_cache[0] is None:
                active_cache[0] = self._score_program(
                    self.program.active_directory(), suite, smoke_request,
                )
            active_result = active_cache[0]
            candidate_result["active_score"] = active_result["score"]
            candidate_result["active_model_calls"] = active_result.get("model_calls", 0)
            candidate_result["passed"] = self._beats(candidate_result, active_result)
        except (ValueError, RuntimeError, subprocess.SubprocessError, BudgetExceeded) as exc:
            candidate_result = {"passed": False, "kind": "failed", "error_type": type(exc).__name__}
        self.program.record_validation(candidate, candidate_result)
        self.log.emit("candidate_validated", candidate=candidate.id, result=candidate_result)
        return candidate_result

    def _program_action(self, action: Action, request: dict, pool: list[tuple[Candidate, dict]],
                        current_task_id: str, active_cache: list[dict | None]) -> dict:
        if action.type == "ROLLBACK_AGENT":
            result = self.program.rollback(action.payload.get("revision"))
            active_cache[0] = None
            return result
        candidate = self.program.stage(action)
        validation = self._validate_candidate(candidate, request, current_task_id, active_cache)
        if self.config.mode == "evolution":
            pool.append((candidate, validation))
            return {"changed": False, "candidate": candidate.id,
                    "validation": self._public_validation(validation),
                    "status": "archived"}
        if validation["passed"]:
            activated = self.program.activate(candidate, validation)
            active_cache[0] = None
            activated["validation"] = self._public_validation(validation)
            return activated
        return {"changed": False, "candidate": candidate.id,
                "validation": self._public_validation(validation),
                "status": "rejected"}

    def _score_state(self, state_view: dict, suite: list[dict]) -> dict:
        scorer = getattr(self.benchmark, "score_validation", None)
        if not scorer:
            raise ValueError("benchmark supplies validation cases without score_validation")
        scores = []
        for case in suite:
            request = json.loads(json.dumps(case["request"], ensure_ascii=False))
            request["phase"] = "validation"
            request["mode"] = self.config.mode
            request["phase_guidance"] = "Solve the replay case without modifying persistent state."
            request["state"] = state_view
            request.setdefault("history", [])
            request.setdefault("agent_program", None)
            request.setdefault("budget", self.cost.view())
            request.setdefault("prices", {
                "decision": self.config.decision_cost,
                "task_action": self.config.task_action_cost,
                "self_action": self.config.self_action_cost,
                "program_run": self.config.program_run_cost,
            })
            action = self._backend_decide(request, "self", "state_validation_decision")
            score = scorer(case, action) if action.type in request.get("actions", {}) else 0.0
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise ValueError("validation score must be numeric")
            scores.append(float(score))
        return {"kind": "task-replay", "cases": len(scores),
                "score": sum(scores) / len(scores), "scores": scores}

    def _state_action(self, action: Action, current_task_id: str) -> dict:
        preview = self.state.preview(action)
        suite = self._validation_suite(current_task_id)
        if not suite:
            validation = {"passed": False, "kind": "no-behavior-validation", "cases": 0,
                          "score": None, "active_score": None}
        else:
            try:
                candidate = self._score_state(
                    {"revision": self.state.revision, "artifacts": preview["artifacts"]}, suite,
                )
                active = self._score_state(self.state.view(), suite)
                validation = {**candidate, "active_score": active["score"],
                              "passed": candidate["score"] >= active["score"]}
            except (ValueError, RuntimeError, BudgetExceeded) as exc:
                validation = {"passed": False,
                              "kind": ("budget-exhausted" if isinstance(exc, BudgetExceeded)
                                       else "failed"), "cases": 0,
                              "score": None, "active_score": None,
                              "error_type": type(exc).__name__}
        self.log.emit("state_candidate_validated", task_id=current_task_id,
                      action=action.to_dict(), result=validation)
        if validation["passed"]:
            committed = self.state.apply(action, validation)
            committed["validation"] = self._public_validation(validation)
            return committed
        return {"changed": False, "revision": self.state.revision,
                "status": "rejected", "validation": self._public_validation(validation)}

    def _task(self, task) -> tuple[dict, dict]:
        start_task, start_self = self.cost.task_units, self.cost.self_units
        observation = self.benchmark.begin(task)
        initial_observation = observation
        trajectory: list[dict] = []
        feedback: dict = {}
        reward, done = 0.0, False
        status, changes, invalid = "step_limit", 0, 0
        program_changes = 0
        candidate_pool: list[tuple[Candidate, dict]] = []
        active_validation_cache: list[dict | None] = [None]
        self.log.emit("task_started", task_id=task.id, evaluation=task.evaluation, observation=observation)
        phases = ["act"]
        if self.config.mode in {"fixed-loop", "open-ended"}:
            phases.append("improve")
        elif self.config.mode == "self-action":
            phases.append("reflect")
        elif self.config.mode == "evolution":
            phases.append("search")
        for phase in phases:
            if phase != "act":
                if status in {"budget_exhausted", "backend_error"}:
                    break
                # No improver call can be made once its own budget is unavailable.
                if self.cost.self_limit - self.cost.self_units < self.config.decision_cost:
                    self.log.emit("improvement_skipped", task_id=task.id, reason="self_budget")
                    break
            limit = (self.config.max_steps if phase == "act" else
                     self.config.evolution_candidates if phase == "search" else self.config.improvement_steps)
            for phase_step in range(limit):
                request = self._request(
                    task.id, phase, observation, trajectory, feedback, phase_step,
                )
                try:
                    action = self._decide(request)
                except BudgetExceeded as exc:
                    if phase == "act" or self.cost.limit - self.cost.total < self.config.decision_cost:
                        status = "budget_exhausted"
                    self.log.emit("budget_exhausted", task_id=task.id, phase=phase, reason=str(exc))
                    break
                except Exception as exc:
                    status = "backend_error"
                    self.log.emit("backend_error", task_id=task.id, phase=phase, error_type=type(exc).__name__)
                    break
                if phase != "act" and action.type == "NOOP":
                    break
                category = "self" if phase != "act" or action.is_self else "task"
                price = self.config.self_action_cost if category == "self" else self.config.task_action_cost
                try:
                    self.cost.charge(price, category)
                except BudgetExceeded as exc:
                    result = {"error": str(exc)}
                    self.log.emit("action_rejected", task_id=task.id, action=action.to_dict(), result=result)
                    trajectory.append({"action": action.to_dict(), "result": result})
                    if self.cost.limit - self.cost.total < price:
                        status = "budget_exhausted"
                        break
                    # SI-only exhaustion must not prevent ordinary task actions.
                    if phase != "act":
                        break
                    continue
                if category == "self":
                    self.cost.self_actions += 1
                else:
                    self.cost.environment_actions += 1
                try:
                    if action.type not in request["actions"]:
                        raise ValueError("action unavailable in this mode/phase/budget")
                    if action.type in PROGRAM_SCHEMAS:
                        result = self._program_action(
                            action, request, candidate_pool, task.id, active_validation_cache,
                        )
                        program_changes += int(result["changed"])
                    elif action.is_self:
                        result = self._state_action(action, task.id)
                        changes += int(result["changed"])
                    else:
                        transition = self.benchmark.execute(action)
                        observation, feedback = transition.observation, transition.feedback
                        done = transition.done
                        # Adapter reward is a terminal task score, not an incremental reward.
                        reward = transition.reward if done else 0.0
                        result = asdict(transition)
                except ValueError as exc:
                    invalid += 1
                    result = {"error": str(exc)}
                trajectory.append({"action": action.to_dict(), "result": result})
                self.log.emit("action", task_id=task.id, phase=phase, action=action.to_dict(), result=result,
                              cost=self.cost.view())
                if phase == "act" and done:
                    status = "completed"
                    break
        if self.config.mode == "evolution" and candidate_pool:
            eligible = [(candidate, result) for candidate, result in candidate_pool if result.get("passed")]
            if eligible:
                # Stable tie-breaking makes identical experiments reproducible.
                winner, validation = max(
                    eligible, key=lambda item: (item[1].get("score") is not None,
                                                item[1].get("score") or 0.0,
                                                -item[1].get("model_calls", 0), item[0].id)
                )
                result = self.program.activate(winner, validation)
                program_changes += 1
                for candidate, _ in candidate_pool:
                    self.program.record_selection(
                        candidate, candidate.id == winner.id,
                        "best task score, then fewer delegated model calls",
                    )
                self.log.emit("candidate_selected", candidate=winner.id, result=result)
            else:
                for candidate, _ in candidate_pool:
                    self.program.record_selection(candidate, False, "active program retained")
                self.log.emit("active_program_retained", program=self.program.view(),
                              candidates=[candidate.id for candidate, _ in candidate_pool])
        row = {"task_id": task.id, "evaluation": task.evaluation, "attempted": True,
               "completed": done, "reward": reward, "status": status,
               "task_units": self.cost.task_units - start_task, "self_units": self.cost.self_units - start_self,
               "state_changes": changes, "program_changes": program_changes,
               "invalid_actions": invalid, "revision": self.state.revision,
               "program_revision": self.program.revision if self.program else None}
        self.log.emit("task_finished", **row, feedback=feedback)
        return row, {"task_id": task.id, "observation": initial_observation,
                     "trajectory": trajectory, "feedback": feedback}
