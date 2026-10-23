"""Load the two upstream agents in the registered EvoArena Harbor runtime.

The upstream ModularRSI fork does not expose this campaign's offline verifier
CLI option. Extending Harbor's agent import path lets the installed EvoArena
Harbor own task loading and grading while the upstream ModularRSI agent and
modules run unmodified.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import harbor.agents

VENDOR_AGENTS = Path(__file__).parent / "vendor/ModularRSI/src/harbor/agents"
harbor.agents.__path__.append(str(VENDOR_AGENTS.resolve()))
META_AGENTS = Path(__file__).parent / "vendor/meta-harness/reference_examples/terminal_bench_2"
if str(META_AGENTS.resolve()) not in sys.path:
    sys.path.insert(0, str(META_AGENTS.resolve()))

from harbor.agents.terminus_2_modular.agent import Terminus2Modular  # noqa: E402


class ModularEvoAgent(Terminus2Modular):
    """Upstream modular solver, imported by the campaign's Harbor."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._evo_model_calls = 0

    async def run(self, instruction, environment, context):
        import litellm
        from unittest.mock import patch

        original = litellm.acompletion

        async def capped(*args, **kwargs):
            if self._evo_model_calls >= 15:
                raise RuntimeError("common_model_call_budget_exhausted")
            self._evo_model_calls += 1
            kwargs.setdefault("max_tokens", 32768)
            kwargs.setdefault("timeout", 2700)
            return await original(*args, **kwargs)

        with patch.object(litellm, "acompletion", capped):
            return await super().run(instruction, environment, context)


from agents.baseline_kira import AgentHarness  # noqa: E402


def normalize_kira_tool_calls(tool_calls: list[dict] | None) -> list[dict]:
    """Accept Qwen's string or object command arguments in KIRA's list parser."""
    normalized = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict) or function.get("name") != "execute_commands":
            normalized.append(call)
            continue
        raw = function.get("arguments", "{}")
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            normalized.append(call)
            continue
        if not isinstance(arguments, dict):
            normalized.append(call)
            continue
        commands = arguments.get("commands", [])
        if isinstance(commands, str):
            try:
                commands = json.loads(commands)
            except json.JSONDecodeError:
                pass
        if isinstance(commands, (str, dict)):
            commands = [commands]
        if not isinstance(commands, list):
            commands = []
        clean = []
        for command in commands:
            if isinstance(command, str):
                command = {"keystrokes": command, "duration": 1.0}
            if not isinstance(command, dict):
                continue
            command = dict(command)
            command["keystrokes"] = str(command.get("keystrokes") or "")
            try:
                command["duration"] = float(command.get("duration", 1.0))
            except (TypeError, ValueError):
                command["duration"] = 1.0
            clean.append(command)
        arguments = dict(arguments, commands=clean)
        normalized.append(dict(call, function=dict(function, arguments=json.dumps(arguments))))
    return normalized


class MetaEvoAgent(AgentHarness):
    """Upstream KIRA harness with the common call and output caps."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._evo_model_calls = 0
        self._evo_commands = 0

    async def _execute_commands(self, commands, session):
        if self._evo_commands + len(commands) > 120:
            raise RuntimeError("common_command_budget_exhausted")
        self._evo_commands += len(commands)
        return await super()._execute_commands(commands, session)

    def _parse_tool_calls(self, tool_calls):
        return super()._parse_tool_calls(normalize_kira_tool_calls(tool_calls))

    async def run(self, instruction, environment, context):
        # KIRA calls LiteLLM directly for both tool and image requests and
        # bypasses Terminus2.llm_call_kwargs. The scoped shim counts actual
        # provider requests, including retries, and supplies the common cap.
        import litellm
        from unittest.mock import patch

        original = litellm.acompletion

        async def capped(*args, **kwargs):
            if self._evo_model_calls >= 15:
                raise RuntimeError("common_model_call_budget_exhausted")
            self._evo_model_calls += 1
            kwargs["max_tokens"] = 32768
            kwargs["timeout"] = 2700
            return await original(*args, **kwargs)

        with patch.object(litellm, "acompletion", capped):
            return await super().run(instruction, environment, context)
