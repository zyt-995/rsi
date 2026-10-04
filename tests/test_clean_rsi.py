import asyncio
import json
from types import SimpleNamespace

import pytest

from rsi_harness.clean_rsi import (
    INITIAL_CONTROLLER,
    atomic_json,
    commit,
    improve,
    initialize,
    load_payload,
    select_skills,
    validate_control,
    validate_files,
    validate_skills,
)

SKILL = "# Dependency diagnosis\n## When\nDependency import errors.\n## Steps\nInspect the interpreter and dependency versions.\n## Cautions\nDo not assume another task uses the same paths."


def test_chain_states_are_independent_and_versions_immutable(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    initial = initialize(one, "skill-rsi")
    changed = commit(one, "skill-rsi", {"dependency-diagnosis": SKILL}, "first", 1)
    assert load_payload(one, initial) == {}
    assert load_payload(one, changed) == {"dependency-diagnosis": SKILL}
    assert initialize(two, "skill-rsi")["revision"] == 0
    with pytest.raises(ValueError, match="collision"):
        commit(one, "skill-rsi", {}, "oops", 1)
    selected, text = select_skills(load_payload(one, changed), "Fix dependency errors")
    assert selected == ["dependency-diagnosis"] and SKILL in text


@pytest.mark.parametrize(
    "value",
    [
        {"../escape.py": "", "agent.py": ""},
        {"agent.py": "def broken("},
        {"agent.py": "", "/etc/x": ""},
    ],
)
def test_harness_rejects_invalid_files(value):
    with pytest.raises((ValueError, SyntaxError)):
        validate_files(value)


@pytest.mark.parametrize(
    "value",
    [
        {"commands": [{"keystrokes": "ls", "duration": 200}]},
        {"complete": "false"},
        {"keep_recent_pairs": 0},
        {"state": "not-a-dict"},
    ],
)
def test_controller_cannot_escape_host_protocol(value):
    with pytest.raises(ValueError):
        validate_control(value)


def test_skill_format_is_checked():
    assert validate_skills({"dependency-diagnosis": SKILL})
    with pytest.raises(ValueError):
        validate_skills({"answer": "The answer is 42"})


def test_cached_improvement_is_committed_once_without_calling_model(tmp_path):
    store = tmp_path / "state"
    folder = store / "improvements" / "first"
    initialize(store, "skill-rsi")
    atomic_json(
        folder / "response.json",
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "reason": "Reusable diagnosis",
                                "skills": {"dependency-diagnosis": SKILL},
                            }
                        )
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "wall_seconds": 1,
        },
    )
    report = improve(
        store, "skill-rsi", "first", "task", tmp_path, 0, "invalid-url", "test"
    )
    assert report["accepted"] and report["after"]["revision"] == 1
    assert (
        improve(store, "skill-rsi", "first", "task", tmp_path, 0, "invalid-url", "test")
        == report
    )
    assert initialize(store, "skill-rsi")["revision"] == 1


def test_interrupted_improvement_does_not_resample(tmp_path):
    atomic_json(tmp_path / "improvements/first/request_started.json", {"time": 1})
    result = improve(
        tmp_path, "skill-rsi", "first", "task", tmp_path, 0, "invalid-url", "test"
    )
    assert result["status"] == "rejected" and "not_retried" in result["error"]


def test_native_control_executes_retry_commands_and_vetoes_completion(monkeypatch):
    from rsi_harness.clean_terminal_agent import CleanTerminalAgent, Terminus2Baseline

    calls, executed, hooks = [], [], []

    async def model(self, chat, prompt, original_instruction, session):
        calls.append(prompt)
        return [], True, "", "done", "finish", SimpleNamespace(content="done")

    monkeypatch.setattr(Terminus2Baseline, "_handle_llm_interaction", model)
    agent = CleanTerminalAgent.__new__(CleanTerminalAgent)
    agent.event = lambda *a, **kw: None

    async def hook(kind, **fields):
        hooks.append(kind)
        if kind == "after_model" and len(calls) == 1:
            return {
                "commands": [{"keystrokes": "public-test\n", "duration": 0.1}],
                "complete": False,
                "retry_prompt": "Fix the failure before finishing",
            }
        return fields

    async def execute(commands, session):
        executed.extend(c.keystrokes for c in commands)
        return False, "public test failure"

    agent.hook = hook
    agent._execute_commands = execute
    result = asyncio.run(
        agent._handle_llm_interaction(
            SimpleNamespace(messages=[]), "solve", "task", None
        )
    )
    assert executed == ["public-test\n"]
    assert len(calls) == 2 and "public test failure" in calls[1]
    assert result[1] is True and hooks == ["before_model", "after_model"] * 2


def test_model_output_budget_stops_solving_but_returns_for_official_grading(
    monkeypatch,
):
    from harbor.llms.base import OutputLengthExceededError

    from rsi_harness.clean_terminal_agent import CleanTerminalAgent, Terminus2Baseline

    async def exhausted(self):
        raise OutputLengthExceededError("exhausted after the allowed recovery")

    monkeypatch.setattr(Terminus2Baseline, "_run_agent_loop", exhausted)
    agent = CleanTerminalAgent.__new__(CleanTerminalAgent)
    events = []
    agent.event = lambda *args, **kwargs: events.append((args, kwargs))
    asyncio.run(agent._run_agent_loop())
    assert agent.rsi_stop_reason.startswith("OutputLengthExceededError:")
    assert events[0][0] == ("agent_stopped",)


@pytest.mark.skipif(
    not __import__("os").environ.get("RSI_DOCKER_TEST"),
    reason="explicit Docker integration",
)
def test_real_sandbox_controller_runs_changed_flow(tmp_path):
    from rsi_harness.clean_rsi import protocol_smoke

    code = INITIAL_CONTROLLER.replace(
        'out["complete"] = r["complete"]',
        'out["complete"] = r["complete"] and out["state"].get("checked", False)\n'
        '    out["state"]["checked"] = True\n'
        '    out["commands"] = [{"keystrokes": "printf check\\n", "duration": 0.1}]',
    )
    directory = tmp_path / "code"
    directory.mkdir()
    (directory / "agent.py").write_text(code)
    report = protocol_smoke(directory)
    after = [c for c in report["cases"] if c["hook"] == "after_model"]
    assert any(c["output"]["complete"] is False for c in after)
    assert any(c["output"]["complete"] is True for c in after)
    assert all(
        c["output"]["commands"][0]["keystrokes"] == "printf check\n" for c in after
    )


@pytest.mark.skipif(
    not __import__("os").environ.get("RSI_DOCKER_TEST"),
    reason="explicit Docker integration",
)
def test_real_sandbox_rejects_task_specific_infinite_completion_veto(tmp_path):
    from rsi_harness.clean_rsi import protocol_smoke

    code = INITIAL_CONTROLLER.replace(
        'out["complete"] = r["complete"]',
        'out["complete"] = r["complete"] and "chess" not in r["instruction"]',
    )
    directory = tmp_path / "code"
    directory.mkdir()
    (directory / "agent.py").write_text(code)
    with pytest.raises(ValueError, match="repeatedly vetoes"):
        protocol_smoke(directory, "Read the chess board and save its best move.")
