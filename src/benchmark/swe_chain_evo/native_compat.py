"""Benchmark result bridges; upstream search, scoring and gates stay intact."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest.mock import patch

from .common import read
from .meta_private import NativeMetaRunner, isolate_legacy_trials


@contextmanager
def canonical_trials(job):
    """Give upstream directory-based parsers full task IDs, even after truncation.

    Build one link per physical trial, before aggregation. Two tasks sharing a
    truncated prefix must never collapse into the same task. Raw artifacts are
    neither renamed nor rewritten.
    """
    job = Path(job)
    with tempfile.TemporaryDirectory(prefix=".swe-task-ids-", dir=job.parent) as directory:
        view = Path(directory)
        for number, trial in enumerate(sorted(job.iterdir())):
            if not trial.is_dir() or "__" not in trial.name:
                continue
            task = trial.name.rsplit("__", 1)[0]
            result = trial / "result.json"
            if result.exists():
                try:
                    task = read(result).get("task_name") or task
                except (ValueError, OSError):
                    pass  # Upstream still counts a corrupt/missing result as 0.
            if not isinstance(task, str) or Path(task).name != task or task in (".", ".."):
                raise ValueError("invalid task identity in native result")
            (view / f"{task}__{number:06d}").symlink_to(trial.resolve(), target_is_directory=True)
        yield view


def install_meta_parsers(native):
    rewards, metrics = native.parse_job_results, native.parse_trial_metrics

    def parse_rewards(job, expected_trials=None):
        with canonical_trials(job) as view:
            return rewards(view, expected_trials=expected_trials)

    def parse_metrics(job):
        with canonical_trials(job) as view:
            return metrics(view)

    native.parse_job_results = parse_rewards
    native.parse_trial_metrics = parse_metrics
    return native


def meta_update(runner, index, parent, rows, output):
    from methods.evo_fifty import native_meta
    original = native_meta.load_native

    def load(workspace):
        native = install_meta_parsers(original(workspace))
        isolate_legacy_trials(runner.root)
        original_prompt = native.render_task_prompt

        def render(*args, **kwargs):
            return original_prompt(*args, **kwargs) + (
                "\nSWE adapter visibility: read public trial result.json and agent/ traces "
                "in the direct task-named job directories, plus evo-history/. "
                "The evo-trials symlinks point to coordinator-private execution data and "
                "are deliberately inaccessible. Hidden grading files, test patches and "
                "reference solutions are unavailable; use scalar task scores and public trajectories.\n")
        native.render_task_prompt = render
        return native

    with patch.object(native_meta, "load_native", load):
        return native_meta.update(NativeMetaRunner(runner), index, parent, rows, output)


def project_native_summary(summary):
    """Restore the crash signal caught by the common accounting wrapper.

    Formal grading keeps its actual 0/1 result. Native evolution sees the same
    missing reward / exception it would see if the original solver had raised.
    """
    if summary is None or not summary.trial_dir:
        return summary
    path = Path(summary.trial_dir) / "result.json"
    if not path.exists():
        return summary
    raw = read(path)
    error = ((raw.get("agent_result") or {}).get("metadata") or {}).get("agent_exception")
    if not error:
        return summary
    return replace(summary, reward=None, exception_type=error["type"],
                   exception_message=error.get("message"))


class NativeModularRunner:
    def __init__(self, runner):
        self.runner = runner

    def __getattr__(self, key):
        return getattr(self.runner, key)

    def run(self, *args, **kwargs):
        result = self.runner.run(*args, **kwargs)
        if result.get("exception"):
            return {**result, "reward": None}
        return result


def modular_update(runner, index, parent, rows, output):
    from methods.evo_fifty import modular
    from harbor.agents.terminus_2_modular.self_evo import online_evo, trajectory_analysis
    original = trajectory_analysis.summarize_trial
    original_evolve = online_evo.run_online_evo
    original_gate = online_evo._gate_and_promote

    def summarize(*args, **kwargs):
        return project_native_summary(original(*args, **kwargs))

    async def evolve(**kwargs):
        # Record the same temperature actually enforced by the common gateway.
        kwargs["solver_temperature"] = runner.config["model"]["temperature"]
        return await original_evolve(**kwargs)

    async def gate(**kwargs):
        cfg = runner.config["arms"]["modularrsi"]
        kwargs.update(max_solver_turns=runner.config["solve"]["max_agent_turns"],
                      max_editor_turns=cfg["max_editor_turns"], review_max_turns=cfg["review_max_turns"],
                      max_gate_repairs=cfg["max_gate_repairs"])
        return await original_gate(**kwargs)

    with patch.object(trajectory_analysis, "summarize_trial", summarize), \
         patch.object(online_evo, "summarize_trial", summarize), \
         patch.object(online_evo, "run_online_evo", evolve), \
         patch.object(online_evo, "_gate_and_promote", gate):
        return modular.update(NativeModularRunner(runner), index, parent, rows, output)
