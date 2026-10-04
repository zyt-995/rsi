from __future__ import annotations

from dataclasses import asdict, dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CostTracker:
    """Trusted ledger. Units are configurable action prices, NOT token estimates."""

    limit: int
    self_limit: int
    task_units: int = 0
    self_units: int = 0
    decisions: int = 0
    backend_calls: int = 0
    environment_actions: int = 0
    self_actions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_reports: int = 0
    backend_seconds: float = 0.0
    program_runs: int = 0
    program_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.limit < 0 or self.self_limit < 0:
            raise ValueError("budgets must be nonnegative")

    @property
    def total(self) -> int:
        return self.task_units + self.self_units

    def charge(self, amount: int, category: str) -> None:
        if type(amount) is not int or amount < 0 or category not in {"task", "self"}:
            raise ValueError("invalid charge")
        if self.total + amount > self.limit:
            raise BudgetExceeded("total budget exhausted")
        if category == "self" and self.self_units + amount > self.self_limit:
            raise BudgetExceeded("self-improvement budget exhausted")
        if category == "self":
            self.self_units += amount
        else:
            self.task_units += amount

    def record_usage(self, usage: dict) -> None:
        if not isinstance(usage, dict):
            raise ValueError("usage must be an object")
        for key in ("input_tokens", "output_tokens"):
            if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                raise ValueError(f"{key} must be a nonnegative integer")
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        if "input_tokens" in usage and "output_tokens" in usage:
            self.usage_reports += 1

    def view(self) -> dict:
        return {
            **asdict(self),
            "total_units": self.total,
            "remaining_units": self.limit - self.total,
            "remaining_self_units": self.self_limit - self.self_units,
            # Direct generated-program decisions consume decision units but have
            # no provider token report. Completeness therefore concerns only
            # calls that actually crossed the model-backend boundary.
            "token_usage_complete": self.usage_reports == self.backend_calls,
        }
