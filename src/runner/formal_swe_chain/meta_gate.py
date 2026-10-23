"""A fresh native proposal session after the interrupted development check."""
import argparse
import copy
from pathlib import Path
import secrets
import shutil
import threading
import time
import traceback

from swe_chain_evo.common import ROOT, atomic, read, sha, existing_imports
existing_imports()
from swe_chain_evo.trial import TrialRunner
from swe_chain_evo.native_compat import meta_update
from swe_chain_evo.campaign import source_pins as core_pins
from methods.evo_fifty.gateway import Server
from methods.evo_fifty.native_meta import VENDOR
from .budget import Broker


def prepare(source, root):
    if (root / "configuration.json").exists():
        return
    root.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(read(source / "configuration.json"))
    config.update(configuration_revision="fresh-native-meta-development-check",
                  development_ledger_path=str(source / "development-spend.sqlite"),
                  experiment_purpose="fresh proposal session and deployment integration; development only")
    config["runtime"].update(gateway_port=8059)
    atomic(root / "configuration.json", config)
    stream = read(source / "task-stream.json")
    atomic(root / "task-stream.json", stream)
    for i, entry in enumerate(stream):
        atomic(root / "entries" / f"{i}.json", entry)
    (root / "tokenizer.json").symlink_to(source / "tokenizer.json")
    source_state = read(source / "native-campaign.json")
    rows = copy.deepcopy(source_state["arms"]["meta-harness"]["rows"][:2])
    atomic(root / "native-campaign.json", {"development_only": True, "arms": {"meta-harness": {
        "rows": rows, "parent": str(VENDOR / "agents/baseline_kira.py"), "candidate": None}}})
    source_job = source / "native-meta-workspace/jobs/evo_000001/incumbent-t1"
    workspace = root / "native-meta-workspace"
    shutil.copytree(VENDOR, workspace, ignore=shutil.ignore_patterns("jobs", "logs", "__pycache__", "*.pyc"))
    job = workspace / "jobs/evo_000001/incumbent-t1"
    # Reuse only the already-scored public incumbent job. Hidden grading paths
    # and the interrupted proposer session are excluded from the fresh sandbox.
    shutil.copytree(source_job, job, ignore=shutil.ignore_patterns("evo-trials", ".swe-task-ids-*"))
    atomic(root / "provenance.json", {"source_development_run": str(source),
        "reused_incumbent_validation": True, "reused_formal_results": [r["result_sha256"] for r in rows],
        "incumbent_validation_replayed": False, "old_proposer_session_reused": False,
        "shared_development_ledger": str(source / "development-spend.sqlite"),
        "shared_lifetime_ledger": config["spend"]["ledger_path"]})


def run(root):
    config = read(root / "configuration.json")
    broker = Broker(root, Path(config["credential_file"]).read_text().strip(), config)
    runner = TrialRunner(root, broker, config["runtime"]["gateway_port"])
    pins = {**core_pins(root), **{str(p): sha(p) for p in (ROOT / "formal_swe_chain").glob("*.py")}}
    # Pin the gate's execution modules, not unrelated preparation tools that can
    # be developed in parallel while this native session is running.
    pins = {p: v for p, v in pins.items() if "/formal_swe_chain/" not in p
            or Path(p).name in ("__init__.py", "budget.py", "meta_gate.py")}
    atomic(root / "native-source-pins.json", pins)
    runner.pins = pins
    server = Server(("127.0.0.1", runner.port), broker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = read(root / "native-campaign.json")
    item = state["arms"]["meta-harness"]
    output = root / "arms/meta-harness/steps/000001/update"
    atomic(root / "gate-status.json", {"state": "proposing", "started": time.time(), "development_only": True})
    try:
        decision = meta_update(runner, 1, Path(item["parent"]), item["rows"], output)
        summary = root / "native-meta-workspace/logs/evo_000001/evolution_summary.jsonl"
        records = [__import__("json").loads(line) for line in summary.read_text().splitlines() if line.strip()] if summary.exists() else []
        candidates = [r for r in records if r.get("agent") not in (None, "incumbent")]
        # Promotion is a method outcome; the integration gate requires an
        # actual submitted and evaluated candidate, including rejected ones.
        passed = bool(candidates)
        value = {"state": "passed" if passed else "no_candidates", "passed": passed,
                 "proposal_records": records, "decision": decision, "development_only": True}
        if passed:
            unseen = runner.run("meta-harness", 2, root / "arms/meta-harness/steps/000002/formal",
                                candidate=decision["candidate"])
            value.update(unseen_task=unseen, passed=not unseen["exception"] and unseen["usage_complete"])
            value["state"] = "passed" if value["passed"] else "deployment_error"
        atomic(root / "gate-status.json", value)
    except BaseException as exc:
        atomic(root / "gate-status.json", {"state": "error", "passed": False, "development_only": True,
            "error": {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}})
        raise
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "runs/development-four-arms-20261015-v1")
    parser.add_argument("--root", type=Path, default=ROOT / "runs/meta-fresh-gate-20261015-v1")
    args = parser.parse_args()
    prepare(args.source.resolve(), args.root.resolve())
    run(args.root.resolve())
