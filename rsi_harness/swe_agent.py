"""Host-side three-arm coding agent for the native SWE-Chain task lifecycle.

Only shell commands run in the benchmark container. Model credentials, RSI
state, and generated harness code remain outside the task container.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

from .clean_rsi import (
    SOLVE,
    ControlExecutor,
    initialize,
    load_payload,
    select_skills,
)

SYSTEM = """You are a coding agent upgrading a Python package. Work only in /app/code.
The benchmark task instructions are in the user message and /app/v*_specs.md.
Reply with one JSON object: {"analysis": "brief reasoning", "plan": "next step",
"commands": [{"keystrokes": "shell command", "duration": 0}], "complete": false}.
Use up to 8 shell commands per response. Shell commands run as root in a
single-UID, network-isolated task container. You cannot access hidden tests, gold versions, credentials,
or future upgrade specs. Inspect the current package, make changes, and run
public checks. Set complete=true when this phase is done; commands in that
response still execute. Do not create or modify files outside /app/code.
"""


class SWEAgent:
    name = "CleanSWEAgent"

    def __init__(self, model: str, arm: str, store: Path | None,
                 base_url: str, token: str, timeout: int = 7200):
        self.model = model
        self.arm = arm
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self.current_task = None
        self.instruction = ""
        self.skills = ""
        self.head = None
        self.controller = None
        self.code_dir = None
        self.state = {}
        self.calls = 0
        self.commands = 0
        self.history = []
        self.events = []
        self.started = 0.0
        self.stop_reason = None
        self.controller_error = None
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def setup_container(self, container) -> None:
        # The solver itself runs on the host; no CLI or API key enters Docker.
        pass

    def begin_task(self, task_id: str, instruction: str) -> None:
        self.close_task()
        self.current_task = task_id
        self.instruction = instruction
        self.state = {}
        self.calls = self.commands = 0
        self.history = []
        self.events = []
        self.started = time.time()
        self.stop_reason = self.controller_error = None
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.head = None
        self.skills = ""
        if self.arm == "baseline":
            return
        if self.store is None:
            raise ValueError("RSI arm requires a chain-local store")
        self.head = initialize(self.store, self.arm)
        payload = load_payload(self.store, self.head)
        if self.arm == "skill-rsi":
            _selected, self.skills = select_skills(payload, instruction)
        elif self.arm == "harness-rsi":
            self.code_dir = (self.store / "versions"
                             / f"{self.head['revision']:06d}" / "code")
            self.controller = ControlExecutor(timeout=15)
            self.controller.bind(self.code_dir.parent)
        else:
            raise ValueError("unknown RSI arm")

    def close_task(self) -> None:
        if self.controller is not None:
            self.controller.close()
            self.controller = None

    def hook(self, kind: str, **fields) -> dict:
        if self.controller is None:
            return fields
        request = {"hook": kind, "instruction": self.instruction,
                   "state": self.state,
                   "remaining_model_calls": SOLVE["max_model_calls"] - self.calls,
                   **fields}
        try:
            output = self.controller.run(self.code_dir, request)
            required = {
                "before_model": ("prompt",),
                "after_model": ("commands", "complete"),
                "after_tools": ("output",),
            }[kind]
            if any(key not in output for key in required):
                raise ValueError("missing required controller output")
        except (OSError, ValueError, RuntimeError) as exc:
            self.controller_error = f"{type(exc).__name__}: {exc}"
            raise ValueError(self.controller_error) from exc
        self.state = output.get("state", self.state)
        self.events.append({"source": "agent", "message": f"harness {kind}",
                            "observation": output})
        return output

    def _model_call(self, messages: list[dict]) -> dict:
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": messages,
                "max_tokens": SOLVE["max_tokens"],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=300) as response:
            result = json.load(response)
        usage = result.get("usage") or {}
        self.usage["input"] += usage.get("prompt_tokens") or 0
        self.usage["output"] += usage.get("completion_tokens") or 0
        self.usage["cache_read"] += usage.get("prompt_cache_hit_tokens") or 0
        return result

    @staticmethod
    def _parse(response: dict) -> dict:
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("truncated model response")
        value = json.loads(choice["message"]["content"])
        if not isinstance(value, dict) or type(value.get("complete")) is not bool:
            raise ValueError("invalid agent response")
        commands = value.get("commands")
        if not isinstance(commands, list) or len(commands) > 8:
            raise ValueError("invalid command list")
        for command in commands:
            if not isinstance(command, dict) or not isinstance(command.get("keystrokes"), str):
                raise TypeError("invalid command")
            if len(command["keystrokes"]) > 20000:
                raise ValueError("command too long")
            command.setdefault("duration", 0)
        value.setdefault("analysis", "")
        value.setdefault("plan", "")
        return value

    def _execute(self, container, commands: list[dict]) -> str:
        outputs = []
        for command in commands:
            self.commands += 1
            try:
                result = subprocess.run(
                    ["docker", "exec", "-u", "root", "-w", "/app", container.name,
                     "bash", "-lc", command["keystrokes"]],
                    capture_output=True, text=True, errors="replace", timeout=300,
                    check=False,
                )
                output = (result.stdout + result.stderr)[-12000:]
                outputs.append(f"$ {command['keystrokes']}\nExit {result.returncode}\n{output}")
            except subprocess.TimeoutExpired:
                outputs.append(f"$ {command['keystrokes']}\nCommand timed out after 300s")
        return "\n\n".join(outputs)[-30000:]

    def run(self, prompt: str, container, live_log_path: str) -> dict:
        if not self.current_task:
            raise RuntimeError("begin_task required")
        started = time.time()
        usage_start = dict(self.usage)
        phase_events = []
        success, error = True, None
        if self.skills:
            prompt += ("\n\nReusable skills from earlier upgrade steps "
                       "(current spec takes precedence):\n" + self.skills)
        self.history.append({"role": "user", "content": prompt})
        with Path(live_log_path).open("a") as log:
            while True:
                if self.calls >= SOLVE["max_model_calls"] or self.commands >= SOLVE["max_commands"]:
                    self.stop_reason = "budget_exhausted"
                    break
                if time.time() - self.started >= SOLVE["timeout"]:
                    self.stop_reason = "task_timeout"
                    break
                try:
                    before = self.hook("before_model", prompt=self.history[-1]["content"])
                    messages = [{"role": "system", "content": SYSTEM}]
                    messages += self.history[:-1]
                    messages.append({"role": "user", "content": before["prompt"]})
                    self.calls += 1
                    response = self._model_call(messages)
                    try:
                        value = self._parse(response)
                    except (ValueError, TypeError, KeyError, IndexError) as first_error:
                        if self.calls >= SOLVE["max_model_calls"]:
                            raise ValueError(f"invalid model JSON at call limit: {first_error}") from first_error
                        raw = response.get("choices", [{}])[0].get("message", {}).get("content")
                        retry_messages = messages + [
                            {"role": "assistant", "content": raw if isinstance(raw, str) else ""},
                            {"role": "user", "content": (
                                "Your previous response was invalid. Return exactly ONE JSON "
                                "object with analysis, plan, commands (an array), and complete "
                                "(a boolean). No second object or text after it."
                            )},
                        ]
                        self.calls += 1
                        response = self._model_call(retry_messages)
                        try:
                            value = self._parse(response)
                        except (ValueError, TypeError, KeyError, IndexError) as retry_error:
                            raise ValueError(
                                f"invalid model JSON after one retry: {retry_error}"
                            ) from retry_error
                    after = self.hook("after_model", commands=value["commands"],
                                      complete=value["complete"],
                                      analysis=str(value["analysis"]),
                                      plan=str(value["plan"]))
                    commands = after["commands"]
                    complete = after["complete"]
                    if len(commands) + self.commands > SOLVE["max_commands"]:
                        self.stop_reason = "command_budget_exhausted"
                        break
                    output = self._execute(container, commands) if commands else ""
                    if commands:
                        output = self.hook("after_tools", output=output)["output"]
                    event = {"source": "agent", "message": value,
                             "tool_calls": commands, "observation": output,
                             "time": time.time()}
                    phase_events.append(event)
                    self.events.append(event)
                    log.write(json.dumps(event, ensure_ascii=False) + "\n")
                    log.flush()
                    self.history.append({"role": "assistant", "content": json.dumps(value)})
                    if complete and not after.get("retry_prompt"):
                        break
                    feedback = after.get("retry_prompt") or "Continue."
                    self.history.append({"role": "user", "content": feedback + "\n\n" + output})
                except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
                    # Never silently score an empty phase as successful.
                    success = False
                    error = f"{type(exc).__name__}: {exc}"
                    self.stop_reason = "model_or_controller_error" if isinstance(
                        exc, (ValueError, TypeError, KeyError, IndexError)
                    ) else "transport_error"
                    break
        return {"elapsed_seconds": round(time.time() - started, 1),
                "success": success, "error": error, "stop_reason": self.stop_reason,
                "token_usage": {key: value - usage_start[key]
                                for key, value in self.usage.items()},
                "trajectory": phase_events}
