"""Versioned, fixed-after-task skill and executable harness improvement.

Generated Python is executed only by ControlExecutor's isolated Docker worker.
The host owns budgets, result validation, version selection and task order.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path, PurePosixPath

from .program import DockerProgramExecutor

SCHEMA = "clean-rsi-v1"
ARMS = ("baseline", "skill-rsi", "harness-rsi")
SOLVE = {
    "max_turns": 15,
    "max_model_calls": 15,
    "max_commands": 120,
    "max_tokens": 32768,
    "max_output_retries": 1,
    "timeout": 7200,
}
IMPROVE = {
    "calls": 1,
    "max_tokens": 4096,
    "timeout": 600,
    "temperature": 0.2,
    "trace_chars": 24000,
    "state_bytes": 32768,
}

INITIAL_CONTROLLER = """import json, sys
r = json.load(sys.stdin)
out = {"state": r.get("state", {})}
if r["hook"] == "before_model":
    out["prompt"] = r["prompt"]
elif r["hook"] == "after_model":
    out["commands"] = r["commands"]
    out["complete"] = r["complete"]
elif r["hook"] == "after_tools":
    out["output"] = r["output"]
json.dump(out, sys.stdout)
"""

CONTROL_SPEC = """The executable harness is called on EVERY solver round, not just task start.
agent.py reads one JSON object from stdin and prints one JSON object to stdout.
All hooks receive: hook, instruction, state (per-task JSON state), remaining_model_calls.
Return state to persist within this task (max 8192 JSON bytes); reset each new task.
before_model also receives prompt. Return prompt; optionally keep_recent_pairs >= 1
to retain the original instruction and that many most-recent conversation pairs.
after_model receives commands [{keystrokes: str, duration: number}], complete: bool,
analysis: str, plan: str. Return commands and complete. You can insert/replace/drop
terminal commands and veto completion. Optionally return retry_prompt: str:
your commands are then executed and another model call sees retry_prompt + tool output.
This consumes the SAME 15-call solver budget; use sparingly.
Completion guards must be bounded: at most two consecutive completion vetoes,
tracked in state, then allow completion. Never request another model call when
remaining_model_calls is zero. Preserve useful pending commands when adding checks.
after_tools receives output: str. Return output to control what the next model call sees.
Terminal output includes echoed shell commands; distinguish actual result lines
from command text when detecting failures or verification markers.
Helpers in other .py files can be imported. No network, host files, or model calls from
the sandbox. The task filesystem is NOT mounted in this sandbox; inspect it only
via returned terminal commands and their subsequent after_tools output.
Never access hidden grader files, tests mounted for grading, reward files, future
tasks, credentials, or task answers. Public tests authored/available during solving
are allowed. Change general workflow, context processing, tool routing or completion
logic; do not merely embed a task-specific answer or a static advice paragraph.
Retain valid behavior for all three hooks and arbitrary tasks. Avoid inventing test
paths/commands not discovered during solving. No third-party Python dependencies.
"""


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def validate_skills(skills) -> dict:
    if not isinstance(skills, dict) or len(skills) > 16:
        raise ValueError("skills must be a dictionary of at most 16 skills")
    for name, text in skills.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise ValueError("invalid skill id")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty skill")
        if any(
            section not in text
            for section in ("# ", "## When", "## Steps", "## Cautions")
        ):
            raise ValueError("skill requires name, When, Steps, Cautions")
    if len(json.dumps(skills).encode()) > IMPROVE["state_bytes"]:
        raise ValueError("skill library exceeds budget")
    return skills


def validate_files(files) -> dict:
    if not isinstance(files, dict) or "agent.py" not in files or len(files) > 16:
        raise ValueError("harness must include agent.py, at most 16 files")
    if len(json.dumps(files).encode()) > IMPROVE["state_bytes"]:
        raise ValueError("harness exceeds budget")
    for name, code in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or any(p in ("", ".", "..") for p in name.split("/")):
            raise ValueError("invalid file path")
        if (
            path.suffix not in (".py", ".json", ".md", ".txt")
            or name == "metadata.json"
        ):
            raise ValueError("invalid file type")
        if not isinstance(code, str):
            raise TypeError("file must contain text")
        if path.suffix == ".py":
            ast.parse(code, filename=name)  # Parse only; never execute on host.
    return files


def validate_control(value) -> dict:
    if not isinstance(value, dict) or set(value) - {
        "state",
        "prompt",
        "commands",
        "complete",
        "output",
        "retry_prompt",
        "keep_recent_pairs",
    }:
        raise ValueError("invalid controller response fields")
    state = value.get("state", {})
    if not isinstance(state, dict) or len(json.dumps(state).encode()) > 8192:
        raise ValueError("invalid per-task state")
    for field in ("prompt", "output", "retry_prompt"):
        if field in value and (
            not isinstance(value[field], str) or len(value[field]) > 200000
        ):
            raise ValueError(f"invalid {field}")
    if "complete" in value and type(value["complete"]) is not bool:
        raise ValueError("complete must be boolean")
    if "keep_recent_pairs" in value and (
        type(value["keep_recent_pairs"]) is not int
        or not 1 <= value["keep_recent_pairs"] <= 15
    ):
        raise ValueError("invalid history limit")
    if "commands" in value:
        commands = value["commands"]
        if not isinstance(commands, list) or len(commands) > 120:
            raise ValueError("invalid commands")
        for cmd in commands:
            if not isinstance(cmd, dict) or set(cmd) != {"keystrokes", "duration"}:
                raise ValueError("invalid command fields")
            if (
                not isinstance(cmd["keystrokes"], str)
                or len(cmd["keystrokes"]) > 200000
            ):
                raise ValueError("invalid command text")
            if (
                type(cmd["duration"]) not in (int, float)
                or not 0 <= cmd["duration"] <= 60
            ):
                raise ValueError("invalid command duration")
    return value


class ControlExecutor(DockerProgramExecutor):
    def validate_output(self, output):
        return validate_control(output)


def initial_payload(arm: str):
    return {"agent.py": INITIAL_CONTROLLER} if arm == "harness-rsi" else {}


def initialize(store: Path, arm: str) -> dict:
    head = store / "HEAD.json"
    if not head.exists():
        commit(store, arm, initial_payload(arm), "initial", 0)
    return read_json(head)


def commit(store: Path, arm: str, payload: dict, task: str, revision: int) -> dict:
    meta = {
        "schema": SCHEMA,
        "arm": arm,
        "revision": revision,
        "sha256": digest(payload),
        "after_task": task,
    }
    directory = store / "versions" / f"{revision:06d}"
    if directory.exists():
        prior = read_json(directory / "payload.json")
        if prior != payload:
            raise ValueError("immutable revision collision")
    else:
        directory.mkdir(parents=True)
        atomic_json(directory / "payload.json", payload)
        if arm == "harness-rsi":
            for name, text in payload.items():
                target = directory / "code" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        else:
            for name, text in payload.items():
                target = directory / "skills" / f"{name}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        atomic_json(directory / "metadata.json", meta)
    atomic_json(store / "HEAD.json", meta)
    return meta


def load_payload(store: Path, head: dict) -> dict:
    payload = read_json(store / "versions" / f"{head['revision']:06d}" / "payload.json")
    if digest(payload) != head["sha256"]:
        raise ValueError("version checksum mismatch")
    return payload


def select_skills(
    payload: dict, instruction: str, limit: int = 4, char_budget: int = 12000
):
    words = set(re.findall(r"[a-z0-9]+", instruction.lower()))
    ranked = sorted(
        payload,
        key=lambda k: (
            -len(words & set(re.findall(r"[a-z0-9]+", (k + " " + payload[k]).lower()))),
            k,
        ),
    )
    chosen, pieces, size = [], [], 0
    for name in ranked[:limit]:
        text = payload[name]
        if size + len(text) <= char_budget:
            chosen.append(name)
            pieces.append(text)
            size += len(text)
    return chosen, "\n\n".join(pieces)


def protocol_smoke(directory: Path, completed_instruction: str = "") -> dict:
    executor = ControlExecutor(timeout=15)
    samples = [
        {
            "hook": "before_model",
            "prompt": "Inspect the public project and fix its bug.",
        },
        {
            "hook": "after_model",
            "commands": [{"keystrokes": "printf hello\\n", "duration": 0.1}],
            "complete": False,
            "analysis": "Inspect",
            "plan": "Inspect",
        },
        {"hook": "after_tools", "output": "Exit code 1: public test failed"},
        {
            "hook": "after_model",
            "commands": [],
            "complete": True,
            "analysis": "Done",
            "plan": "Finish",
        },
    ]
    state = {}
    evidence = []
    try:
        executor.bind(directory.parent)
        for sample in samples:
            request = {
                "instruction": "A synthetic public task",
                "state": state,
                "remaining_model_calls": 10,
                **sample,
            }
            out = executor.run(directory, request)
            required = {
                "before_model": ("prompt",),
                "after_model": ("commands", "complete"),
                "after_tools": ("output",),
            }[sample["hook"]]
            if any(key not in out for key in required):
                raise ValueError("missing hook output")
            state = out.get("state", state)
            evidence.append({"hook": sample["hook"], "input": request, "output": out})
        # Exercise task-specific branches without running task commands or hidden tests.
        # A successful synthetic tool result must not cause an endless completion veto.
        for instruction in dict.fromkeys(
            ["A synthetic public task", completed_instruction]
        ):
            if not instruction:
                continue
            state = {}

            def invoke(hook, remaining=5, instruction=instruction, **fields):
                nonlocal state
                request = {
                    "hook": hook,
                    "instruction": instruction,
                    "state": state,
                    "remaining_model_calls": remaining,
                    **fields,
                }
                out = executor.run(directory, request)
                required = {
                    "before_model": ("prompt",),
                    "after_model": ("commands", "complete"),
                    "after_tools": ("output",),
                }[hook]
                if any(key not in out for key in required):
                    raise ValueError("missing hook output in liveness check")
                state = out.get("state", state)
                evidence.append({"hook": hook, "input": request, "output": out})
                return out

            for attempt in range(3):
                invoke("before_model", prompt="The requested work is complete.")
                out = invoke(
                    "after_model",
                    commands=[],
                    complete=True,
                    analysis="Public verification succeeded.",
                    plan="Finish",
                )
                if out["complete"] and not out.get("retry_prompt"):
                    break
                invoke(
                    "after_tools",
                    output="Exit code 0: public verification passed. FILE_EXISTS. Non-empty output verified.",
                )
            else:
                raise ValueError(
                    "controller repeatedly vetoes completion (liveness check)"
                )
            exhausted = invoke(
                "after_model",
                remaining=0,
                commands=[],
                complete=True,
                analysis="Budget exhausted",
                plan="Finish",
            )
            if exhausted.get("retry_prompt"):
                raise ValueError("controller retries after model budget exhaustion")
    finally:
        executor.close()
    return {"passed": True, "cases": evidence}


def public_trace(trial: Path) -> str:
    trajectory = read_json(trial / "agent/trajectory.json", {})
    records = []
    for step in trajectory.get("steps", []):
        if step.get("source") == "agent":
            records.append(
                {
                    k: step[k]
                    for k in ("message", "tool_calls", "observation")
                    if k in step
                }
            )
    # This is the solver's own trajectory, never grader stdout or hidden tests.
    extra = trial / "agent/rsi_events.jsonl"
    if extra.exists():
        for line in extra.read_text().splitlines():
            event = json.loads(line)
            if event.get("event") in (
                "tools",
                "controller_retry",
                "controller_error",
                "agent_stopped",
            ):
                records.append(event)
    text = json.dumps(records, ensure_ascii=False)
    return text[-IMPROVE["trace_chars"] :]


def proposal_prompt(arm, payload, instruction, trial, reward):
    surface = (
        """Return JSON {"reason": "why", "skills": {"skill-id": "markdown", ...}}.
The skills object completely replaces the current library. Add, refine or merge
reusable skills grounded in this trajectory, including lessons from failure.
Each skill needs # Name, ## When, ## Steps, ## Cautions. Do not store the task answer,
long copied solutions, hardcoded task paths or generic filler. Generalize parameters.
Keep useful existing skills; at most 16 skills, 32768 JSON bytes total.
"""
        if arm == "skill-rsi"
        else 'Return JSON {"reason": "why", "files": {"agent.py": "complete source", ...}}.\n'
        "files completely replaces the current harness. At most 32768 JSON bytes.\n"
        + CONTROL_SPEC
    )
    return (
        "You are performing the mandatory post-task improvement phase. Model weights are frozen.\n"
        "Only this completed task and its observed trajectory are available. A basic check gates\n"
        "deployment; improvement is measured on subsequent unseen tasks. Propose one useful\n"
        "revision. If none is justified return the unchanged content and explain why.\n"
        + surface
        + "\nCURRENT CONTENT:\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\nCOMPLETED TASK:\n"
        + instruction[:8000]
        + "\nPUBLIC FINAL SCORE: "
        + str(reward)
        + "\nOBSERVED SOLVER TRAJECTORY:\n"
        + public_trace(trial)
    )


def improve(
    store: Path,
    arm: str,
    task: str,
    instruction: str,
    trial: Path,
    reward: float,
    base_url: str,
    model: str,
) -> dict:
    folder = store / "improvements" / task
    final = folder / "result.json"
    if final.exists():
        return read_json(final)
    before_path = folder / "before.json"
    if not before_path.exists():
        atomic_json(before_path, initialize(store, arm))
    before = read_json(before_path)
    payload = load_payload(store, before)
    proposal_file = folder / "response.json"
    report = {
        "schema": SCHEMA,
        "task": task,
        "arm": arm,
        "before": before,
        "after": before,
        "accepted": False,
        "budget": IMPROVE,
        "started": time.time(),
    }
    try:
        if not proposal_file.exists():
            if (folder / "request_started.json").exists():
                raise RuntimeError("interrupted_request_not_retried")
            prompt = proposal_prompt(arm, payload, instruction, trial, reward)
            request = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": IMPROVE["max_tokens"],
                "temperature": IMPROVE["temperature"],
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            }
            atomic_json(folder / "request.json", request)
            atomic_json(folder / "request_started.json", {"time": time.time()})
            http = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(request).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer EMPTY",
                },
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            started = time.time()
            with opener.open(http, timeout=IMPROVE["timeout"]) as stream:
                response = json.load(stream)
            response["wall_seconds"] = time.time() - started
            atomic_json(proposal_file, response)
        response = read_json(proposal_file)
        report["usage"] = response.get("usage")
        report["model_wall_seconds"] = response.get("wall_seconds")
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("proposal_output_truncated")
        text = choice["message"]["content"].strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        proposal = json.loads(text)
        new = proposal["skills" if arm == "skill-rsi" else "files"]
        (validate_skills if arm == "skill-rsi" else validate_files)(new)
        report["reason"] = str(proposal.get("reason", ""))[:4000]
        if new == payload:
            report["status"] = "no_change"
        else:
            candidate = folder / "candidate"
            candidate.mkdir(exist_ok=True)
            atomic_json(folder / "proposal.json", proposal)
            if arm == "harness-rsi":
                for name, code in new.items():
                    path = candidate / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(code)
                report["validation"] = protocol_smoke(candidate, instruction)
            else:
                report["validation"] = {"passed": True, "skill_count": len(new)}
            # Commit is idempotent after a crash: immutable revision has the same payload.
            after = commit(store, arm, new, task, before["revision"] + 1)
            report.update(accepted=True, after=after, status="accepted")
    except Exception as exc:  # noqa: BLE001 -- Candidate/model failures must be recorded without resampling.
        report.update(status="rejected", error=f"{type(exc).__name__}: {exc}")
    report["usage_complete"] = report.get("usage") is not None
    report["finished"] = time.time()
    atomic_json(final, report)
    return report
