from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class ScriptedAgent:
    """Deterministic infrastructure fixture, not an LLM or a research baseline."""

    def __init__(self):
        self._search_counts: dict[str, int] = {}

    def manifest(self) -> dict:
        return {"backend": "scripted", "synthetic": True, "version": 1}

    def decide(self, request: dict[str, Any]) -> dict:
        artifacts = request["state"]["artifacts"]
        try:
            memory = json.loads(artifacts.get("MEMORY.md", "") or "{}")
        except (ValueError, TypeError):
            memory = {}
        if not isinstance(memory, dict):
            memory = {}
        route = None
        stale = False
        for step in reversed(request["trajectory"]):
            result = step["result"]
            if result.get("feedback", {}).get("stale_answer"):
                stale = True
                break
            if "route" in result.get("observation", {}):
                route = result["observation"]["route"]
                break
        if request["phase"] == "search":
            index = request.get("phase_step", self._search_counts.get(request["task_id"], 0))
            self._search_counts[request["task_id"]] = index + 1
            if route and index < 3:
                variants = (("bad", "/invalid-route"), ("direct", route), ("delegate", route))
                name, candidate_route = variants[index]
                action = self._propose(candidate_route, name)
            else:
                action = {"type": "NOOP", "reason": "No useful candidate evidence"}
        elif request["phase"] in {"improve", "reflect"}:
            program = request.get("agent_program")
            source = (program or {}).get("files", {}).get("workflow.py", "")
            if program and route and route not in source:
                action = self._propose(route, "direct")
            elif not program and route and memory.get("billing") != route:
                action = self._write(route)
            else:
                action = {"type": "NOOP", "reason": "No new reusable evidence"}
        else:
            if route is None and not stale:
                route = memory.get("billing")
                for previous in reversed(request["history"]):
                    if route:
                        break
                    for step in reversed(previous["trajectory"]):
                        route = step["result"].get("observation", {}).get("route")
                        if route:
                            break
            can_write = ("WRITE_ARTIFACT" in request["actions"] and
                         request["budget"]["remaining_self_units"] >= request["prices"]["self_action"])
            if route is None:
                action = {"type": "LOOKUP", "reason": "No current route, or prior route was rejected"}
            elif can_write and memory.get("billing") != route:
                action = self._write(route)
            else:
                action = {"type": "ANSWER", "payload": {"route": route}, "reason": "Use available route"}
        return {"action": action, "usage": {"input_tokens": 0, "output_tokens": 0}}

    @staticmethod
    def _write(route: str) -> dict:
        return {"type": "WRITE_ARTIFACT", "payload": {
            "path": "MEMORY.md", "content": json.dumps({"billing": route}) + "\n",
        }, "reason": "Persist the route observed during this task"}

    @staticmethod
    def _propose(route: str, variant: str) -> dict:
        if variant == "delegate":
            workflow = (
                "def decide(request):\n"
                "    return {'delegate': {'instructions': 'use current evidence', 'context': {}}}\n"
            )
        else:
            workflow = (
                "import json\n"
                f"ROUTE = {route!r}\n"
                "def decide(request):\n"
                "    phase = request.get('phase')\n"
                "    if phase not in {'act', 'validation'}:\n"
                "        return {'delegate': {'instructions': '', 'context': {}}}\n"
                "    evidence = None\n"
                "    stale = False\n"
                "    for step in reversed(request.get('trajectory', [])):\n"
                "        result = step.get('result', {})\n"
                "        stale = stale or result.get('feedback', {}).get('stale_answer', False)\n"
                "        evidence = evidence or result.get('observation', {}).get('route')\n"
                "    try:\n"
                "        memory = json.loads(request.get('state', {}).get('artifacts', {}).get('MEMORY.md') or '{}')\n"
                "    except Exception:\n"
                "        memory = {}\n"
                "    chosen = evidence or memory.get('billing') or ROUTE\n"
                "    if stale and evidence is None:\n"
                "        return {'action': {'type': 'LOOKUP', 'payload': {}, 'reason': 'refresh stale route'}}\n"
                "    return {'action': {'type': 'ANSWER', 'payload': {'route': chosen}, 'reason': 'compiled policy'}}\n"
            )
        agent = (
            "import json, sys\n"
            "from workflow import decide\n"
            "request = json.load(sys.stdin)\n"
            "json.dump(decide(request), sys.stdout)\n"
        )
        return {"type": "PROPOSE_AGENT", "payload": {
            "label": f"{variant} route policy", "files": {
                "agent.py": agent, "workflow.py": workflow,
                "prompts/policy.md": "Prefer validated persistent evidence.\n",
            },
        }, "reason": f"Propose {variant} executable policy"}


class CommandAgent:
    """One JSON request on stdin, one JSON response on stdout, per decision.

    This executes a trusted, user-supplied bridge, not generated agent code.
    The bridge owns model authentication, prompting and provider usage reporting.
    """

    def __init__(self, command: list[str], timeout: float = 60):
        if not command or timeout <= 0:
            raise ValueError("command and positive timeout are required")
        self.command, self.timeout = command, timeout

    def manifest(self) -> dict:
        return {"backend": "command", "argv": self.command, "timeout_seconds": self.timeout}

    def decide(self, request: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix="rsi-decision-") as directory:
            result = subprocess.run(
                self.command, input=json.dumps(request, ensure_ascii=False),
                text=True, encoding="utf-8", capture_output=True,
                cwd=directory, timeout=self.timeout, check=False,
            )
        if result.returncode:
            # Do not copy stderr into experiment logs: provider credentials may be there.
            raise RuntimeError(f"agent bridge exited with code {result.returncode}")
        if len(result.stdout.encode("utf-8")) > 1_000_000:
            raise ValueError("agent response exceeds 1 MB")
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("agent response must be a JSON object")
        return response


class CodexAgent:
    """Codex CLI backend with a fresh, ephemeral session for every decision.

    The backend receives only the runner request and has a read-only empty working
    directory. Persistent information must therefore travel through AgentState.
    """

    _SCHEMA = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {
            "action": {
                "type": "object", "additionalProperties": False,
                "required": ["type", "payload_json", "reason"],
                "properties": {
                    "type": {"type": "string"},
                    "payload_json": {"type": "string"},
                    "reason": {"type": "string"},
                },
            }
        },
    }

    def __init__(self, model: str, reasoning_effort: str = "medium", timeout: float = 600,
                 executable: str = "codex"):
        if not model or timeout <= 0:
            raise ValueError("model and positive timeout are required")
        self.model, self.reasoning_effort, self.timeout, self.executable = (
            model, reasoning_effort, timeout, executable
        )

    def manifest(self) -> dict:
        return {"backend": "codex-cli", "model": self.model,
                "reasoning_effort": self.reasoning_effort, "timeout_seconds": self.timeout,
                "fresh_session_per_decision": True}

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "You are the policy inside a controlled continual-agent experiment. "
            "Choose exactly one action from request.actions. Respect each payload schema. "
            "Use persistent state when relevant. Do not invent unavailable actions. "
            "Encode the action payload object as JSON in action.payload_json; use '{}' when empty. "
            "Return JSON matching the supplied schema.\n\nREQUEST:\n" +
            json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        )
        with tempfile.TemporaryDirectory(prefix="rsi-codex-") as directory:
            workdir = Path(directory)
            schema = workdir / "response.schema.json"
            output = workdir / "response.json"
            schema.write_text(json.dumps(self._SCHEMA), encoding="utf-8")
            command = [
                self.executable, "exec", "--ephemeral", "--ignore-user-config",
                "--sandbox", "read-only", "--skip-git-repo-check", "--json",
                "--color", "never", "--model", self.model,
                "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
                "--output-schema", str(schema), "--output-last-message", str(output), "-",
            ]
            result = subprocess.run(
                command, input=prompt, text=True, encoding="utf-8", capture_output=True,
                cwd=workdir, timeout=self.timeout, check=False,
                env=self._environment(),
            )
            if result.returncode:
                raise RuntimeError(f"Codex CLI exited with code {result.returncode}")
            response = json.loads(output.read_text(encoding="utf-8"))
            usage = self._extract_usage(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("Codex response must be an object")
        action = response.get("action")
        if not isinstance(action, dict):
            raise ValueError("Codex response has no action object")
        try:
            payload = json.loads(action.pop("payload_json"))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Codex action payload_json is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("Codex action payload_json must encode an object")
        action["payload"] = payload
        response["usage"] = usage
        return response

    @staticmethod
    def _environment() -> dict[str, str]:
        # Keep provider/auth configuration without forwarding arbitrary experiment vars.
        allowed = {"PATH", "CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                   "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        return {key: value for key, value in os.environ.items() if key in allowed}

    @staticmethod
    def _extract_usage(stdout: str) -> dict[str, int]:
        best = {"input_tokens": 0, "output_tokens": 0}
        found = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            stack = [event]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    if isinstance(value.get("input_tokens"), int):
                        candidate = {
                            "input_tokens": value["input_tokens"],
                            "output_tokens": int(value.get("output_tokens", 0)),
                        }
                        if sum(candidate.values()) >= sum(best.values()):
                            best, found = candidate, True
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
        return best if found else {}
