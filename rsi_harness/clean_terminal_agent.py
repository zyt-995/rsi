"""Native Terminal agent with immutable skills or sandboxed per-round control."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from harbor.agents.terminus_2.terminus_2 import Command
from harbor.llms.base import OutputLengthExceededError
from harbor_EvoMem.agents.terminus2_baseline import Terminus2Baseline

from .clean_rsi import (
    SCHEMA,
    SOLVE,
    ControlExecutor,
    atomic_json,
    digest,
    initialize,
    load_payload,
    select_skills,
)


class BudgetExhausted(Exception):
    pass


class CleanTerminalAgent(Terminus2Baseline):
    def __init__(self, rsi_arm="baseline", rsi_store=None, *args, **kwargs):
        if rsi_arm not in ("baseline", "skill-rsi", "harness-rsi"):
            raise ValueError("invalid clean RSI arm")
        self.rsi_arm = rsi_arm
        self.rsi_store = Path(rsi_store) if rsi_store else None
        self.rsi_head = None
        self.rsi_executor = None
        self.rsi_state = {}
        self.rsi_calls = self.rsi_commands = self.rsi_hooks = 0
        self.rsi_runtime_error = None
        self.rsi_stop_reason = None
        self.rsi_instruction = ""
        self.rsi_selected = []
        super().__init__(*args, **kwargs)
        # Count every model call, including Terminus summarization/recovery.
        original_call = self._llm.call

        async def bounded_call(*a, **kw):
            if self.rsi_calls >= SOLVE["max_model_calls"]:
                raise BudgetExhausted("model call budget exhausted")
            self.rsi_calls += 1
            self.event("model_call", count=self.rsi_calls)
            return await original_call(*a, **kw)

        self._llm.call = bounded_call

    def event(self, kind, **fields):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with (self.logs_dir / "rsi_events.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {"event": kind, "time": time.time(), **fields}, ensure_ascii=False
                )
                + "\n"
            )

    async def hook(self, kind, **fields):
        if self.rsi_executor is None:
            return fields
        request = {
            "hook": kind,
            "instruction": self.rsi_instruction,
            "state": self.rsi_state,
            "remaining_model_calls": SOLVE["max_model_calls"] - self.rsi_calls,
            **fields,
        }
        try:
            output = await asyncio.to_thread(
                self.rsi_executor.run, self.rsi_code, request
            )
            required = {
                "before_model": ("prompt",),
                "after_model": ("commands", "complete"),
                "after_tools": ("output",),
            }[kind]
            if any(k not in output for k in required):
                raise ValueError("controller omitted required hook fields")
        except Exception as exc:
            self.rsi_runtime_error = f"{type(exc).__name__}: {exc}"
            self.event("controller_error", hook=kind, error=self.rsi_runtime_error)
            raise BudgetExhausted("controller runtime failure") from exc
        self.rsi_state = output.get("state", self.rsi_state)
        self.rsi_hooks += 1
        self.event(
            "controller_hook",
            hook=kind,
            version=self.rsi_head,
            input=request,
            output=output,
            changed=any(output.get(k, fields[k]) != fields[k] for k in fields),
        )
        return output

    async def _handle_llm_interaction(
        self, chat, prompt, original_instruction="", session=None
    ):
        while True:
            before = await self.hook("before_model", prompt=prompt)
            pairs = before.get("keep_recent_pairs")
            if pairs and len(chat.messages) > (pairs + 1) * 2:
                chat.messages[:] = chat.messages[:2] + chat.messages[-pairs * 2 :]
                chat.reset_response_chain()
            result = await super()._handle_llm_interaction(
                chat, before["prompt"], original_instruction, session
            )
            commands, complete, feedback, analysis, plan, response = result
            # Preserve parser errors; generated code cannot promote malformed output to success.
            if feedback and "ERROR:" in feedback:
                return result
            after = await self.hook(
                "after_model",
                commands=[
                    {"keystrokes": c.keystrokes, "duration": c.duration_sec}
                    for c in commands
                ],
                complete=complete,
                analysis=analysis,
                plan=plan,
            )
            commands = [
                Command(c["keystrokes"], c["duration"]) for c in after["commands"]
            ]
            if after.get("retry_prompt"):
                _timeout, output = await self._execute_commands(commands, session)
                prompt = after["retry_prompt"] + "\n\nTerminal output:\n" + output
                self.event("controller_retry", prompt=prompt)
                continue
            return commands, after["complete"], feedback, analysis, plan, response

    async def _execute_commands(self, commands, session):
        if self.rsi_commands + len(commands) > SOLVE["max_commands"]:
            raise BudgetExhausted("terminal command budget exhausted")
        self.rsi_commands += len(commands)
        timeout, output = await super()._execute_commands(commands, session)
        self.event(
            "tools",
            commands=[
                {"keystrokes": c.keystrokes, "duration": c.duration_sec}
                for c in commands
            ],
            output=output,
        )
        processed = await self.hook("after_tools", output=output)
        return timeout, processed["output"]

    async def _run_agent_loop(self, *args, **kwargs):
        try:
            await super()._run_agent_loop(*args, **kwargs)
        except (BudgetExhausted, OutputLengthExceededError) as exc:
            # This is an agent outcome; official grading still runs on its workspace.
            self.rsi_stop_reason = f"{type(exc).__name__}: {exc}"
            self.event("agent_stopped", reason=self.rsi_stop_reason)

    async def run(self, instruction, environment, context):
        self.rsi_instruction = instruction
        self.rsi_state = {}
        self.rsi_calls = self.rsi_commands = self.rsi_hooks = 0
        if self.rsi_arm != "baseline":
            if self.rsi_store is None:
                raise ValueError("RSI arm requires isolated chain store")
            self.rsi_head = initialize(self.rsi_store, self.rsi_arm)
            payload = load_payload(self.rsi_store, self.rsi_head)
            if self.rsi_arm == "skill-rsi":
                self.rsi_selected, skills = select_skills(payload, instruction)
                if skills:
                    instruction += (
                        "\n\nReusable skills from earlier tasks (current requirements take precedence):\n"
                        + skills
                    )
            else:
                self.rsi_code = (
                    self.rsi_store
                    / "versions"
                    / f"{self.rsi_head['revision']:06d}"
                    / "code"
                )
                self.rsi_executor = ControlExecutor(timeout=15)
                await asyncio.to_thread(self.rsi_executor.bind, self.rsi_code.parent)
        atomic_json(
            self.logs_dir / "rsi_start.json",
            {
                "schema": SCHEMA,
                "arm": self.rsi_arm,
                "version": self.rsi_head,
                "selected_skills": self.rsi_selected,
                "instruction_sha256": digest(instruction),
                "budget": SOLVE,
            },
        )
        try:
            await super().run(instruction, environment, context)
        finally:
            if self.rsi_executor:
                await asyncio.to_thread(self.rsi_executor.close)
            info = {
                "schema": SCHEMA,
                "arm": self.rsi_arm,
                "version": self.rsi_head,
                "model_calls": self.rsi_calls,
                "commands": self.rsi_commands,
                "controller_hooks": self.rsi_hooks,
                "selected_skills": self.rsi_selected,
                "controller_runtime_error": self.rsi_runtime_error,
                "stop_reason": self.rsi_stop_reason,
            }
            context.metadata = {**(context.metadata or {}), "clean_rsi": info}
            atomic_json(self.logs_dir / "rsi_end.json", info)
