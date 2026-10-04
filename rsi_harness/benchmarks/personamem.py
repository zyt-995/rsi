from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ..types import Action, Task, Transition


TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _stable_seed(*parts: Any) -> int:
    payload = "||".join(str(part) for part in parts)
    return int(hashlib.md5(payload.encode("utf-8")).hexdigest()[:8], 16)


def _query(raw: str) -> str:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            value = raw
    return str(value.get("content", "")) if isinstance(value, dict) else str(value)


def _answers(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
        return [str(item) for item in value] if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


class PersonaMemEvo:
    """Direct adapter for the released PersonaMem-Evo 10-person subset.

    It uses the upstream rows, histories, deterministic option shuffling and exact
    MCQ scoring. Context retrieval is a documented local BM25-like adapter rather
    than the upstream A-Mem implementation, so manifest() identifies this as a
    direct adapter rather than an official-runner reproduction.
    """

    name = "evoarena-personamem-direct"

    def __init__(self, root: str | Path, limit: int | None = None, persona_ids: list[str] | None = None,
                 retrieve_k: int = 12, seed: int = 0):
        self.root = Path(root).resolve()
        self.csv_path = self.root / "data" / "personamem-evo-10p.csv"
        if not self.csv_path.is_file():
            raise FileNotFoundError(self.csv_path)
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        if retrieve_k < 1:
            raise ValueError("retrieve_k must be positive")
        wanted = set(str(x) for x in (persona_ids or []))
        with self.csv_path.open(newline="", encoding="utf-8") as stream:
            rows = [row for row in csv.DictReader(stream) if not wanted or row["persona_id"] in wanted]
        self.rows = rows[:limit]
        self.retrieve_k, self.seed = retrieve_k, seed
        self._history_cache: dict[str, list[dict[str, Any]]] = {}
        self._tasks = [Task(f"persona-{i:04d}", {
            "index": i, "persona_id": row["persona_id"], "chain_id": row.get("chain_id", ""),
            "chain_size": int(row.get("chain_size") or 1), "ood_type": row.get("ood_type", ""),
            "ood_difficulty": row.get("ood_difficulty", ""), "oracle_reward": 1.0,
            "change_point": i > 0 and row["persona_id"] != self.rows[i - 1]["persona_id"],
        }) for i, row in enumerate(self.rows)]

    def manifest(self) -> dict[str, Any]:
        digest = hashlib.sha256(self.csv_path.read_bytes()).hexdigest()
        return {"name": self.name, "synthetic": False, "adapter": "direct-bm25-v1",
                "dataset": str(self.csv_path), "dataset_sha256": digest,
                "tasks": len(self.rows), "retrieve_k": self.retrieve_k, "seed": self.seed,
                "limitations": ["retrieval differs from upstream A-Mem runner"]}

    def tasks(self) -> list[Task]:
        return list(self._tasks)

    def actions(self) -> dict[str, dict[str, Any]]:
        return {"ANSWER": {"description": "Choose the best answer", "payload": {"choice": "A|B|C|D"}}}

    def begin(self, task: Task) -> dict[str, Any]:
        index = int(task.id.rsplit("-", 1)[1])
        self._row = self.rows[index]
        observation, option_map, correct_choice = self._observation(index)
        self._option_map, self._correct = option_map, correct_choice
        return observation

    def _observation(self, index: int) -> tuple[dict[str, Any], dict[str, str], str]:
        row = self.rows[index]
        correct = row["correct_answer"]
        options = [correct, *_answers(row.get("incorrect_answers", ""))]
        rng = random.Random(_stable_seed(self.seed, row["persona_id"], row["user_query"], index))
        rng.shuffle(options)
        option_map = {chr(65 + i): value for i, value in enumerate(options)}
        correct_choice = next(letter for letter, value in option_map.items() if value == correct)
        question = _query(row["user_query"])
        retrieval_query = " ".join([question, *options])
        context = self._retrieve(row["chat_history_32k_link"], retrieval_query)
        observation = {
            "instruction": "Use the retrieved conversation evidence to choose the best answer. Reply with ANSWER(choice).",
            "question": question,
            "options": option_map,
            "retrieved_history": context,
            "retrieval": {"method": "bm25-like-v1", "top_k": self.retrieve_k},
        }
        return observation, option_map, correct_choice

    def execute(self, action: Action) -> Transition:
        if action.type != "ANSWER":
            raise ValueError("PersonaMem-Evo accepts only ANSWER")
        choice = str(action.payload.get("choice", "")).strip().upper()
        success = choice == self._correct
        return Transition({"accepted": success}, True, float(success), {"success": success})

    def validation_cases(self, completed_task_ids: list[str], limit: int) -> list[dict[str, Any]]:
        cases = []
        for task_id in completed_task_ids[-limit:]:
            index = int(task_id.rsplit("-", 1)[1])
            observation, _, correct = self._observation(index)
            cases.append({
                "request": {
                    "schema_version": 1, "task_id": f"validation-{task_id}", "phase": "validation",
                    "observation": observation, "trajectory": [], "feedback": {},
                    "actions": self.actions(),
                },
                "correct_choice": correct,
            })
        return cases

    @staticmethod
    def score_validation(case: dict[str, Any], action: Action) -> float:
        choice = str(action.payload.get("choice", "")).strip().upper()
        return float(action.type == "ANSWER" and choice == case["correct_choice"])

    def _load_history(self, relative: str) -> list[dict[str, Any]]:
        if relative not in self._history_cache:
            path = (self.root / "data" / relative).resolve()
            if not path.is_relative_to((self.root / "data").resolve()) or not path.is_file():
                raise FileNotFoundError(f"invalid history path: {relative}")
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value = value.get("chat_history", value.get("conversations", []))
            if not isinstance(value, list):
                raise ValueError(f"history is not a list: {path}")
            self._history_cache[relative] = [item for item in value if isinstance(item, dict)]
        return self._history_cache[relative]

    def _retrieve(self, relative: str, query: str) -> list[dict[str, Any]]:
        messages = self._load_history(relative)
        query_terms = Counter(token.lower() for token in TOKEN_RE.findall(query))
        document_terms = [Counter(token.lower() for token in TOKEN_RE.findall(str(m.get("content", "")))) for m in messages]
        document_frequency = Counter(term for terms in document_terms for term in terms)
        n = max(len(messages), 1)
        ranked = []
        for index, (message, terms) in enumerate(zip(messages, document_terms)):
            length = max(sum(terms.values()), 1)
            score = 0.0
            for term, qtf in query_terms.items():
                tf = terms.get(term, 0)
                if tf:
                    score += qtf * (tf / (tf + 1.2 * (0.25 + 0.75 * length / 100))) * math.log(1 + n / (1 + document_frequency[term]))
            if score > 0:
                ranked.append((score, index, message))
        selected = sorted(ranked, key=lambda item: (-item[0], -item[1]))[:self.retrieve_k]
        selected.sort(key=lambda item: item[1])
        return [{"index": index, "role": message.get("role", "unknown"),
                 "content": str(message.get("content", "")), "score": round(score, 6)}
                for score, index, message in selected]
