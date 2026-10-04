from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


SELF_ACTIONS = frozenset({
    "WRITE_ARTIFACT", "DELETE_ARTIFACT", "ROLLBACK", "PROPOSE_AGENT", "ROLLBACK_AGENT",
})
MODES = ("static", "context-only", "fixed-loop", "self-action", "open-ended", "evolution")


@dataclass(frozen=True)
class Action:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def is_self(self) -> bool:
        return self.type in SELF_ACTIONS

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Action:
        if not isinstance(value, dict) or set(value) - {"type", "payload", "reason"}:
            raise ValueError("action must contain only type, payload, reason")
        kind, payload, reason = value.get("type"), value.get("payload", {}), value.get("reason", "")
        if not isinstance(kind, str) or not isinstance(payload, dict) or not isinstance(reason, str):
            raise ValueError("invalid action types")
        return cls(kind, payload, reason)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Task:
    id: str
    # Evaluation metadata is written to logs, NEVER sent to the agent.
    evaluation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    observation: dict[str, Any]
    done: bool = False
    reward: float = 0.0
    feedback: dict[str, Any] = field(default_factory=dict)


class BenchmarkAdapter(Protocol):
    name: str

    def manifest(self) -> dict[str, Any]: ...

    def tasks(self) -> list[Task]: ...

    def begin(self, task: Task) -> dict[str, Any]: ...

    def actions(self) -> dict[str, dict[str, Any]]: ...

    def execute(self, action: Action) -> Transition: ...


class AgentBackend(Protocol):
    def manifest(self) -> dict[str, Any]: ...

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return {action: {...}, usage: {input_tokens: n, output_tokens: n}}."""
        ...
