import json
from pathlib import Path
import sys
import tempfile
import unittest

from examples.openai_compatible_bridge import parse_content
from rsi_harness.agents import CommandAgent, ScriptedAgent
from rsi_harness.analysis import analyze
from rsi_harness.benchmarks.toy import ShiftingRoutes
from rsi_harness.budget import BudgetExceeded, CostTracker
from rsi_harness.metrics import summarize
from rsi_harness.program import LocalProgramExecutor, ProgramStore
from rsi_harness.runner import RunConfig, Runner
from rsi_harness.state import StateStore
from rsi_harness.types import Action, MODES


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.state = StateStore(self.root)

    def write(self, path, content):
        return self.state.apply(Action("WRITE_ARTIFACT", {"path": path, "content": content}))

    def test_persist_delete_and_rollback_are_versioned(self):
        self.write("skills/lookup.md", "Use current docs\n")
        self.assertEqual(StateStore(self.root).artifacts["skills/lookup.md"], "Use current docs\n")
        self.state.apply(Action("DELETE_ARTIFACT", {"path": "skills/lookup.md"}))
        result = self.state.apply(Action("ROLLBACK", {"revision": 1}))
        self.assertEqual(result["revision"], 3)
        self.assertIn("skills/lookup.md", self.state.artifacts)
        self.assertEqual(len(list((self.root / "revisions").glob("*.json"))), 4)

    def test_reject_path_escape_and_evaluator_edits_without_changes(self):
        for path in ("../evaluator.py", "/tmp/escape", "skills/../../x", "skills//x", "skills/./x", "evaluator.py", "skills\\x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.write(path, "bad")
        self.assertEqual(self.state.revision, 0)

    def test_limit_and_noop_do_not_advance_revision(self):
        self.write("MEMORY.md", "")
        self.assertEqual(self.state.revision, 0)
        with self.assertRaises(ValueError):
            self.write("MEMORY.md", "x" * 65537)
        self.assertEqual(StateStore(self.root).revision, 0)

    def test_invalid_rollback_cannot_load_future_or_bool_revision(self):
        for revision in (-1, 12, True, "0"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.state.apply(Action("ROLLBACK", {"revision": revision}))

    def test_initial_state_must_fit_limit(self):
        with self.assertRaises(ValueError):
            StateStore(self.root / "too-small", max_bytes=1)
        self.assertFalse((self.root / "too-small").exists())


class ProgramTests(unittest.TestCase):
    PROGRAM = (
        "import json, sys\n"
        "r=json.load(sys.stdin)\n"
        "json.dump({'delegate': {'instructions': 'prefer current evidence', 'context': {}}}, sys.stdout)\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProgramStore(Path(self.temp.name) / "program")

    def test_candidate_activation_and_rollback_are_append_only(self):
        candidate = self.store.stage(Action("PROPOSE_AGENT", {
            "label": "delegate v1", "files": {"agent.py": self.PROGRAM, "notes.md": "test"},
        }))
        self.store.record_validation(candidate, {"passed": True, "score": 1.0})
        result = self.store.activate(candidate, {"passed": True, "score": 1.0})
        self.assertEqual(result["revision"], 1)
        rolled = self.store.rollback(0)
        self.assertEqual(rolled["revision"], 2)
        self.assertEqual(len(list(self.store.versions.iterdir())), 3)

    def test_rejects_escape_reserved_metadata_and_missing_entrypoint(self):
        for files in ({"../agent.py": "x"}, {"metadata.json": "{}", "agent.py": "x"}, {"readme.md": "x"}):
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.store.stage(Action("PROPOSE_AGENT", {"files": files}))

    def test_multifile_program_imports_and_source_is_visible(self):
        candidate = self.store.stage(Action("PROPOSE_AGENT", {
            "label": "multi-file", "files": {
                "agent.py": (
                    "import json,sys\nfrom workflow import decide\n"
                    "json.dump(decide(json.load(sys.stdin)),sys.stdout)\n"
                ),
                "workflow.py": (
                    "def decide(request):\n"
                    " return {'delegate': {'instructions': 'from-helper', 'context': {}}}\n"
                ),
                "prompts/policy.md": "policy source\n",
            },
        }))
        output = LocalProgramExecutor().run(candidate.directory, {"phase": "act"})
        self.assertEqual(output["delegate"]["instructions"], "from-helper")
        self.store.activate(candidate, {"passed": True, "score": 1.0})
        view = self.store.view(include_files=True)
        self.assertIn("workflow.py", view["files"])
        self.assertIn("prompts/policy.md", view["files"])
        self.assertFalse(any("__pycache__" in name for name in view["files"]))


class BudgetTests(unittest.TestCase):
    def test_failed_charge_is_atomic_and_self_cap_does_not_block_task(self):
        tracker = CostTracker(3, 1)
        tracker.charge(1, "self")
        with self.assertRaises(BudgetExceeded):
            tracker.charge(1, "self")
        self.assertEqual(tracker.total, 1)
        tracker.charge(2, "task")
        with self.assertRaises(BudgetExceeded):
            tracker.charge(1, "task")
        self.assertEqual(tracker.total, 3)

    def test_usage_unknown_is_not_reported_as_complete(self):
        tracker = CostTracker(4, 2)
        tracker.decisions = 1
        tracker.backend_calls = 1
        tracker.record_usage({})
        self.assertFalse(tracker.view()["token_usage_complete"])
        with self.assertRaises(ValueError):
            tracker.record_usage({"input_tokens": -1})


class BridgeTests(unittest.TestCase):
    def test_openai_compatible_bridge_parses_json_and_fences(self):
        expected = {"action": {"type": "NOOP", "payload": {}, "reason": "done"}}
        self.assertEqual(parse_content(json.dumps(expected)), expected)
        self.assertEqual(parse_content("```json\n" + json.dumps(expected) + "\n```"), expected)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0

    def run_mode(self, mode, backend=None, **kwargs):
        self.counter += 1
        path = self.root / str(self.counter)
        runner = Runner(ShiftingRoutes(seed=7, task_count=12, shift_every=4),
                        backend or ScriptedAgent(), RunConfig(mode=mode, **kwargs), path,
                        program_executor=(LocalProgramExecutor() if mode in {"open-ended", "evolution"} else None))
        summary = runner.run()
        events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
        return runner, summary, events

    def test_modes_and_stale_route_recovery(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                runner, summary, events = self.run_mode(mode)
                self.assertEqual(summary["completed"], 12)
                self.assertEqual(summary["invalid_actions"], 0)
                self.assertEqual(summary["cost"]["input_tokens"], 0)
                self.assertTrue(summary["cost"]["token_usage_complete"])
                if mode in {"static", "context-only", "open-ended", "evolution"}:
                    self.assertEqual(runner.state.revision, 0)
                else:
                    self.assertEqual(runner.state.revision, 3)
                if mode == "static":
                    self.assertEqual(summary["cumulative_reward"], 12)
                elif mode == "evolution":
                    # Population search has a larger, explicitly charged SI cost;
                    # under the common default cap it adapts to two of three shifts.
                    self.assertEqual(summary["cumulative_reward"], 9.5)
                else:
                    self.assertEqual(summary["cumulative_reward"], 11)
                if mode == "open-ended":
                    self.assertEqual(summary["final_program_revision"], 3)
                if mode == "evolution":
                    self.assertGreater(summary["final_program_revision"], 0)
                requests = [e["request"] for e in events if e["event"] == "decision_started"]
                self.assertTrue(all("evaluation" not in r and "regime" not in r["observation"] for r in requests))
                if mode != "context-only":
                    self.assertTrue(all(r["history"] == [] for r in requests))
                else:
                    self.assertTrue(any(r["history"] for r in requests))

    def test_fixed_loop_only_modifies_after_task_and_self_action_inside_task(self):
        for mode in ("fixed-loop", "self-action"):
            _, _, events = self.run_mode(mode)
            changes = [e for e in events if e["event"] == "action" and e["action"]["type"] == "WRITE_ARTIFACT"]
            self.assertTrue(changes)
            self.assertTrue(all(e["phase"] == ("improve" if mode == "fixed-loop" else "act") for e in changes))

    def test_self_action_has_post_terminal_reflection_hook(self):
        class TerminalWriter:
            def manifest(self):
                return {"backend": "terminal-writer"}

            def decide(self, request):
                route = None
                for step in reversed(request["trajectory"]):
                    route = route or step["result"].get("observation", {}).get("route")
                memory = json.loads(request["state"]["artifacts"].get("MEMORY.md") or "{}")
                if request["phase"] == "validation":
                    action = ({"type": "ANSWER", "payload": {"route": memory["billing"]}}
                              if memory.get("billing") else {"type": "LOOKUP"})
                elif request["phase"] == "reflect":
                    action = ({"type": "WRITE_ARTIFACT", "payload": {
                        "path": "MEMORY.md", "content": json.dumps({"billing": route}) + "\n",
                    }} if route and memory.get("billing") != route else {"type": "NOOP"})
                else:
                    action = ({"type": "ANSWER", "payload": {"route": route}}
                              if route else {"type": "LOOKUP"})
                return {"action": action, "usage": {"input_tokens": 0, "output_tokens": 0}}

        runner, summary, events = self.run_mode("self-action", TerminalWriter())
        changes = [e for e in events if e["event"] == "action"
                   and e["action"]["type"] == "WRITE_ARTIFACT"]
        self.assertEqual(runner.state.revision, 3)
        self.assertEqual(summary["completed"], 12)
        self.assertTrue(changes)
        self.assertTrue(all(e["phase"] == "reflect" for e in changes))
        reflection_requests = [e["request"] for e in events
                               if e["event"] == "decision_started"
                               and e["request"]["phase"] == "reflect"]
        self.assertTrue(any(r["feedback"].get("success") for r in reflection_requests))

    def test_budget_exhaustion_counts_all_unattempted_tasks(self):
        _, summary, _ = self.run_mode("static", budget=4)
        self.assertEqual(summary["tasks"], 12)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["cumulative_reward"], 1)
        self.assertEqual(summary["mean_reward"], 1 / 12)
        self.assertEqual(summary["cost"]["total_units"], 4)
        self.assertEqual(summary["status"], "budget_exhausted")

    def test_no_self_budget_still_allows_task_completion(self):
        for mode in ("fixed-loop", "self-action"):
            _, summary, _ = self.run_mode(mode, self_budget=0)
            self.assertEqual(summary["completed"], 12)
            self.assertEqual(summary["final_revision"], 0)
            self.assertEqual(summary["cost"]["self_units"], 0)

    def test_static_rejects_self_changes_even_if_backend_requests_them(self):
        class Writer(ScriptedAgent):
            def decide(self, request):
                return {"action": {"type": "WRITE_ARTIFACT", "payload": {"path": "MEMORY.md", "content": "cheat"}}}
        runner, summary, _ = self.run_mode("static", Writer(), max_steps=2)
        self.assertEqual(runner.state.revision, 0)
        self.assertEqual(summary["invalid_actions"], 24)
        self.assertEqual(summary["cumulative_reward"], 0)

    def test_backend_request_mutation_cannot_mutate_runner_state(self):
        class Mutator(ScriptedAgent):
            def decide(self, request):
                request["state"]["artifacts"]["MEMORY.md"] = "poison"
                request["trajectory"].clear()
                request["actions"]["WRITE_ARTIFACT"] = {}
                return {"action": {"type": "WRITE_ARTIFACT", "payload": {"path": "MEMORY.md", "content": "poison"}}}
        runner, summary, _ = self.run_mode("static", Mutator(), max_steps=1)
        self.assertEqual(runner.state.artifacts["MEMORY.md"], "")
        self.assertEqual(summary["invalid_actions"], 12)

    def test_malformed_backend_stops_with_accounted_decision_and_full_horizon(self):
        class Broken(ScriptedAgent):
            def decide(self, request):
                return {"action": {"type": 123}}
        _, summary, _ = self.run_mode("static", Broken())
        self.assertEqual(summary["status"], "backend_error")
        self.assertEqual(summary["cost"]["decisions"], 1)
        self.assertEqual(summary["tasks"], 12)
        self.assertFalse(summary["cost"]["token_usage_complete"])

    def test_deterministic_results_excluding_wall_clock(self):
        _, first, _ = self.run_mode("self-action")
        _, second, _ = self.run_mode("self-action")
        first["cost"].pop("backend_seconds")
        second["cost"].pop("backend_seconds")
        self.assertEqual(first, second)

    def test_refuses_to_overwrite_existing_run(self):
        runner, _, _ = self.run_mode("static")
        with self.assertRaises(FileExistsError):
            Runner(ShiftingRoutes(), ScriptedAgent(), RunConfig(), runner.output)

    def test_command_bridge_matches_in_process_results(self):
        bridge = Path(__file__).resolve().parents[1] / "examples" / "scripted_bridge.py"
        _, external, _ = self.run_mode("self-action", CommandAgent([sys.executable, str(bridge)]))
        _, internal, _ = self.run_mode("self-action")
        external["cost"].pop("backend_seconds")
        internal["cost"].pop("backend_seconds")
        self.assertEqual(external, internal)

    def test_context_history_obeys_byte_cap(self):
        _, _, events = self.run_mode("context-only", history_bytes=1)
        requests = [e["request"] for e in events if e["event"] == "decision_started"]
        self.assertTrue(all(r["history"] == [] for r in requests))

    def test_analysis_pipeline_writes_machine_readable_and_svg_outputs(self):
        self.run_mode("static")
        self.run_mode("fixed-loop")
        result = analyze([self.root], self.root / "analysis")
        self.assertEqual(result["runs"], 2)
        self.assertEqual(set(result["by_mode"]), {"static", "fixed-loop"})
        for name in ("aggregate.json", "runs.csv", "tasks.csv", "learning-curves.svg"):
            self.assertTrue((self.root / "analysis" / name).is_file())

    def test_open_ended_candidate_is_behavior_validated_activated_and_used(self):
        runner, summary, events = self.run_mode(
            "open-ended", improvement_steps=1, validation_cases=2,
        )
        self.assertEqual(summary["status"], "finished")
        self.assertEqual(summary["final_program_revision"], 3)
        self.assertEqual(runner.program.revision, 3)
        validations = [event for event in events if event["event"] == "candidate_validated"]
        self.assertEqual(len(validations), 3)
        self.assertTrue(all(event["result"]["passed"] for event in validations))
        self.assertTrue(all(event["result"]["cases"] > 0 for event in validations))
        self.assertTrue(any(event["event"] == "program_decision" for event in events))
        self.assertIn("workflow.py", runner.program.files())
        improvement_requests = [event["request"] for event in events
                                if event["event"] == "decision_started"
                                and event["request"]["phase"] == "improve"]
        self.assertTrue(any("files" in request["agent_program"]
                            for request in improvement_requests))

    def test_evolution_builds_same_parent_population_and_selects_winner(self):
        runner, summary, events = self.run_mode(
            "evolution", budget=400, self_budget=220, validation_cases=2,
            evolution_candidates=3,
        )
        candidates = sorted(runner.program.candidates.iterdir())
        self.assertEqual(len(candidates), 9)
        first_population = [json.loads((path / "metadata.json").read_text())
                            for path in candidates[:3]]
        self.assertEqual({item["parent"] for item in first_population}, {0})
        self.assertEqual(sum(item["selection"]["selected"] for item in first_population), 1)
        self.assertTrue(any(item["validation"]["score"] == 0
                            and not item["validation"]["passed"]
                            for item in first_population))
        self.assertEqual(summary["final_program_revision"], 3)
        self.assertEqual(summary["program_changes"], 3)
        search_requests = [event["request"] for event in events
                           if event["event"] == "decision_started"
                           and event["request"]["phase"] == "search"]
        self.assertEqual([r["search"]["slot"] for r in search_requests[:3]], [0, 1, 2])

    def test_program_candidate_without_behavior_cases_is_never_deployed(self):
        class NoReplay(ShiftingRoutes):
            def validation_cases(self, completed_task_ids, limit):
                return []

        path = self.root / "no-replay"
        runner = Runner(
            NoReplay(seed=1, task_count=1, shift_every=1), ScriptedAgent(),
            RunConfig(mode="open-ended", improvement_steps=1), path,
            program_executor=LocalProgramExecutor(),
        )
        summary = runner.run()
        events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
        validation = next(e["result"] for e in events if e["event"] == "candidate_validated")
        self.assertEqual(summary["final_program_revision"], 0)
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["kind"], "no-behavior-validation")


class MetricTests(unittest.TestCase):
    def test_recovery_does_not_cross_next_change_and_missing_oracle_is_null(self):
        rows = [{"task_id": str(i), "evaluation": {"change_point": i in (1, 3)},
                 "attempted": True, "completed": True, "reward": score,
                 "state_changes": 0, "invalid_actions": 0}
                for i, score in enumerate([1, 0.5, 1, 1, 1])]
        metrics = summarize(rows, {})
        self.assertIsNone(metrics["oracle_regret"])
        self.assertTrue(metrics["recovery"][0]["censored"])
        self.assertEqual(metrics["recovery"][1]["tasks_to_recover"], 0)


if __name__ == "__main__":
    unittest.main()
