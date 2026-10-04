from __future__ import annotations

import argparse
import importlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from .agents import CodexAgent, CommandAgent, ScriptedAgent
from .analysis import analyze
from .benchmarks.personamem import PersonaMemEvo
from .benchmarks.registry import preflight
from .benchmarks.toy import ShiftingRoutes
from .runner import RunConfig, Runner
from .state import write_json
from .types import MODES


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamic RSI: reproducible continual-agent experiments")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "demo", "matrix"):
        help_text = {
            "run": "Run one policy",
            "demo": "Compare four RSI paradigms and two controls on the toy benchmark",
            "matrix": "Run a benchmark across modes and seeds, then aggregate results",
        }[name]
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--output", type=Path)
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--tasks", type=int, default=24)
        p.add_argument("--shift-every", type=int, default=8)
        p.add_argument("--budget", type=int, default=200)
        p.add_argument("--self-budget", type=int, default=60)
        p.add_argument("--program-run-cost", type=int, default=1)
        p.add_argument("--max-steps", type=int, default=12)
        p.add_argument("--improvement-steps", type=int, default=2)
        p.add_argument("--validation-cases", type=int, default=3)
        p.add_argument("--evolution-candidates", type=int, default=3)
        p.add_argument("--program-bytes", type=int, default=131072)
        p.add_argument("--program-image", default="python:3.10-slim")
        if name in {"run", "matrix"}:
            if name == "run":
                p.add_argument("--mode", choices=MODES, default="static")
            else:
                p.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
                p.add_argument("--seeds", nargs="+", type=int)
            p.add_argument("--benchmark", choices=("toy", "personamem"), default="toy")
            p.add_argument("--agent-command", help='JSON argv array, e.g. ["python3", "/absolute/path/bridge.py"]')
            p.add_argument("--agent-timeout", type=float, default=60)
            p.add_argument("--codex-model", help="Use the logged-in Codex CLI with this explicit model")
            p.add_argument("--reasoning-effort", default="medium")
            p.add_argument("--persona-root", type=Path,
                           default=Path("benchmarks/vendor/evoarena/EvoMem-PersonaMem-Evo"))
            p.add_argument("--persona-id", action="append", dest="persona_ids")
            p.add_argument("--retrieve-k", type=int, default=12)
            p.add_argument("--benchmark-factory", help="Import a trusted adapter factory: package.module:factory")
            p.add_argument("--benchmark-config", type=Path, help="JSON kwargs passed to the adapter factory")
    report = sub.add_parser("report", help="Print a run's saved summary")
    report.add_argument("run_dir", type=Path)
    analysis = sub.add_parser("analyze", help="Aggregate complete run directories into CSV, JSON and SVG")
    analysis.add_argument("inputs", type=Path, nargs="+")
    analysis.add_argument("--output", type=Path, required=True)
    sub.add_parser("preflight", help="Check locked upstream benchmark checkouts and runtimes")
    args = parser.parse_args()
    try:
        if args.command == "report":
            print((args.run_dir / "summary.json").read_text(encoding="utf-8"))
            return
        if args.command == "analyze":
            result = analyze(args.inputs, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"Artifacts: {args.output.resolve()}")
            return
        if args.command == "preflight":
            result = preflight()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(0 if result["ready"] else 1)
        output = args.output or Path("runs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%f")
        if args.command == "demo":
            # Validate parameters before making the comparison directory.
            _config(args, "static")
            ShiftingRoutes(args.seed, args.tasks, args.shift_every)
            output.mkdir(parents=True, exist_ok=False)
            summaries = []
            for mode in MODES:
                benchmark = ShiftingRoutes(args.seed, args.tasks, args.shift_every)
                summary = Runner(benchmark, ScriptedAgent(), _config(args, mode), output / mode).run()
                summaries.append(summary)
            write_json(output / "comparison.json", summaries)
            print("Synthetic infrastructure check — not evidence of LLM self-improvement.")
            print(f"{'mode':16} {'reward':>8} {'cost':>8} {'SI cost':>8} {'changes':>8}")
            for summary in summaries:
                print(f"{summary['mode']:16} {summary['cumulative_reward']:8.2f} "
                      f"{summary['cost']['total_units']:8} {summary['cost']['self_units']:8} "
                      f"{summary['state_changes']:8}")
        elif args.command == "matrix":
            _validate_experiment_args(args)
            seeds = args.seeds or [args.seed]
            if len(seeds) != len(set(seeds)):
                raise ValueError("--seeds must not contain duplicates")
            output.mkdir(parents=True, exist_ok=False)
            summaries = []
            for seed in seeds:
                for mode in args.modes:
                    run_dir = output / f"seed-{seed}" / mode
                    summary = Runner(
                        _benchmark(args, seed), _backend(args), _config(args, mode), run_dir,
                    ).run()
                    summaries.append({"seed": seed, "run_dir": str(run_dir), **summary})
            write_json(output / "matrix.json", {
                "schema_version": 1, "seeds": seeds, "modes": args.modes, "runs": summaries,
            })
            aggregate = analyze([output], output / "analysis")
            print(json.dumps(aggregate, ensure_ascii=False, indent=2))
        else:
            _validate_experiment_args(args)
            summary = Runner(
                _benchmark(args, args.seed), _backend(args), _config(args, args.mode), output,
            ).run()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if summary["status"] == "backend_error":
                print(f"Backend failed; inspect {output / 'events.jsonl'}")
                raise SystemExit(1)
        print(f"Artifacts: {output.resolve()}")
    except (ValueError, OSError, ImportError, AttributeError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")


def _validate_experiment_args(args) -> None:
    if args.benchmark_config and not args.benchmark_factory:
        raise ValueError("--benchmark-config requires --benchmark-factory")
    if sum(bool(value) for value in (args.agent_command, args.codex_model)) > 1:
        raise ValueError("choose only one of --agent-command and --codex-model")
    if not args.agent_command and not args.codex_model:
        if args.benchmark_factory or args.benchmark != "toy":
            raise ValueError("non-toy benchmarks require --agent-command or --codex-model")


def _benchmark(args, seed: int):
    if args.benchmark_factory:
        module, symbol = args.benchmark_factory.split(":", 1)
        factory = getattr(importlib.import_module(module), symbol)
        kwargs = json.loads(args.benchmark_config.read_text()) if args.benchmark_config else {}
        if "seed" in inspect.signature(factory).parameters:
            kwargs.setdefault("seed", seed)
        return factory(**kwargs)
    if args.benchmark == "personamem":
        return PersonaMemEvo(
            args.persona_root, limit=args.tasks, persona_ids=args.persona_ids,
            retrieve_k=args.retrieve_k, seed=seed,
        )
    return ShiftingRoutes(seed, args.tasks, args.shift_every)


def _backend(args):
    if args.agent_command:
        argv = json.loads(args.agent_command)
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("--agent-command must be a nonempty JSON array of strings")
        return CommandAgent(argv, args.agent_timeout)
    if args.codex_model:
        return CodexAgent(args.codex_model, args.reasoning_effort, args.agent_timeout)
    return ScriptedAgent()


def _config(args, mode: str) -> RunConfig:
    return RunConfig(
        mode=mode, budget=args.budget, self_budget=args.self_budget, max_steps=args.max_steps,
        program_run_cost=args.program_run_cost,
        improvement_steps=args.improvement_steps, validation_cases=args.validation_cases,
        evolution_candidates=args.evolution_candidates, program_bytes=args.program_bytes,
        program_image=args.program_image,
    )
