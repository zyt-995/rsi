from __future__ import annotations

import random

from ..types import Action, Task, Transition


class ShiftingRoutes:
    """Offline fixture: service routes silently change at regime boundaries.

    LOOKUP reveals only the current service's route. ANSWER earns 1 on a first
    attempt, 0.5 after one stale answer, and 0 after two failed answers.
    The agent sees neither a regime ID nor a change-point signal.
    """

    name = "toy-shifting-routes"

    def __init__(self, seed: int = 0, task_count: int = 24, shift_every: int = 8):
        if task_count < 1 or shift_every < 1:
            raise ValueError("task_count and shift_every must be positive")
        self.seed, self.task_count, self.shift_every = seed, task_count, shift_every
        rng = random.Random(seed)
        self._routes = [f"/api/{rng.getrandbits(64):016x}" for _ in range((task_count - 1) // shift_every + 1)]
        self._tasks = [Task(f"task-{i:04d}", {
            "index": i, "regime": i // shift_every,
            "change_point": i > 0 and i % shift_every == 0,
            "oracle_reward": 1.0,
        }) for i in range(task_count)]

    def manifest(self) -> dict:
        return {"name": self.name, "synthetic": True, "seed": self.seed,
                "task_count": self.task_count, "shift_every": self.shift_every}

    def tasks(self) -> list[Task]:
        return list(self._tasks)

    def begin(self, task: Task) -> dict:
        index = next(i for i, known in enumerate(self._tasks) if known.id == task.id)
        self._route = self._routes[index // self.shift_every]
        self._failures, self._done = 0, False
        return {"instruction": "Find the current billing API route, then ANSWER with route.",
                "service": "billing"}

    def actions(self) -> dict:
        return {
            "LOOKUP": {"description": "Read the current billing route", "payload": {}},
            "ANSWER": {"description": "Submit route; at most two wrong answers", "payload": {"route": "string"}},
        }

    def execute(self, action: Action) -> Transition:
        if self._done:
            raise ValueError("task already finished")
        if action.type == "LOOKUP":
            return Transition({"service": "billing", "route": self._route})
        if action.type != "ANSWER":
            raise ValueError("unknown task action")
        success = action.payload.get("route") == self._route
        if not success:
            self._failures += 1
        self._done = success or self._failures >= 2
        reward = (1.0 if self._failures == 0 else 0.5) if success else 0.0
        return Transition(
            {"service": "billing", "accepted": success}, self._done, reward,
            {"success": success, "stale_answer": not success},
        )

    def validation_cases(self, completed_task_ids: list[str], limit: int) -> list[dict]:
        cases = []
        current_index = int(completed_task_ids[-1].rsplit("-", 1)[1])
        current_regime = current_index // self.shift_every
        compatible_ids = [
            task_id for task_id in completed_task_ids
            if int(task_id.rsplit("-", 1)[1]) // self.shift_every == current_regime
        ]
        for task_id in compatible_ids[-limit:]:
            index = int(task_id.rsplit("-", 1)[1])
            route = self._routes[index // self.shift_every]
            cases.append({
                "request": {
                    "schema_version": 1, "task_id": f"validation-{task_id}", "phase": "validation",
                    "observation": {"instruction": "Find the current billing API route, then ANSWER with route.",
                                    "service": "billing"},
                    "trajectory": [],
                    "feedback": {}, "actions": self.actions(),
                },
                "expected_route": route,
            })
        return cases

    @staticmethod
    def score_validation(case: dict, action: Action) -> float:
        return float(action.type == "ANSWER" and action.payload.get("route") == case["expected_route"])
