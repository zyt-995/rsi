"""Zero-model readiness checks, including optional real Harbor plumbing probes."""
import argparse
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from swe_chain_evo.common import atomic, read, sha, docker, docker_env
from . import RELEASE, SESSION
from .contracts import validate, source_pins, budget_snapshot


def inventory(root):
    cfg, arm, stream = validate(root, require_ready=False)
    from harbor.models.task.task import Task
    from formal_swe_chain.grading import map_path
    images = {e["image_id"] for e in stream}
    actual = set(docker("image", "inspect", *sorted(images), "--format", "{{.Id}}", timeout=60).stdout.decode().splitlines())
    if actual != images:
        raise RuntimeError("prepared task image inventory changed")
    evidence = []
    for entry in stream:
        if Task(Path(entry["path"])).checksum != entry["task_checksum"]:
            raise RuntimeError("prepared task checksum changed")
        mapping = map_path(entry)
        frozen = read(mapping)
        if frozen["normalizer_sha256"] != sha(RELEASE / "formal_swe_chain/grading.py"):
            raise RuntimeError("reference test map belongs to a different normalizer")
        if frozen["instance_sha256"] != sha(RELEASE / "data/SWE-Chain-Evo" / entry["instance_path"]):
            raise RuntimeError("reference task data changed")
        evidence.append({"task": entry["task"], "task_checksum": entry["task_checksum"],
                         "image_id": entry["image_id"], "test_id_map": str(mapping), "test_id_map_sha256": sha(mapping)})
    return evidence


def probe(root):
    """Run the original zero-LLM agent and actual normalized result collector."""
    from evoskill_swe.trial import TrialRunner
    from rsi_portable.paths import runner_environment
    root = Path(root)
    cfg, arm, stream = validate(root, require_ready=False)
    entry = stream[0]
    out = root / "preparation/zero-model-plumbing"
    out.mkdir(parents=True, exist_ok=True)
    results = list((out / "trials").glob("*/result.json"))
    if not results:
        env = runner_environment(cfg["runtime"]["gateway_port"])
        for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY"):
            env.pop(key, None)
        env.update(PYTHONPATH=str(RELEASE) + ":" + str(RELEASE / "runtime-project"),
                   PYTHONDONTWRITEBYTECODE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
        env.update(docker_env())
        cmd = [str(RELEASE / "runtime-project/.venv-terminal/bin/harbor"), "trials", "start",
               "--path", entry["path"], "--trials-dir", str(out / "trials"),
               "--env", "swe_chain_evo.environment:OfflineDocker",
               "--agent-import-path", "swe_chain_evo.preflight:NoopAgent",
               "--verifier", "swe_chain_evo.verifier:SWEVerifier",
               "--verifier-kwarg", "entry_file=" + str(root / "entries/0.json"),
               "--verifier-kwarg", "entry_sha256=" + sha(root / "entries/0.json")]
        with (out / "worker.log").open("ab") as log:
            subprocess.run(cmd, cwd=RELEASE, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900, check=True)
    results = list((out / "trials").glob("*/result.json"))
    if len(results) != 1:
        raise RuntimeError("zero-model probe did not return one task result")
    broker = SimpleNamespace(config=cfg, scopes={"offline": {"calls": [], "pending_tokens": 0, "blocked": False}},
                             ledger=SimpleNamespace(snapshot=lambda: {"blocking": []}))
    runner = TrialRunner(root, broker, cfg["runtime"]["gateway_port"])
    atomic(out / "launch.json", {"command": [], "model_calls": 0, "development_only": True})
    source = {"candidate_sha256": None, "modules_hashes": None}
    row = runner._collect(arm, 0, out, "offline_preflight", source, {"offline": True}, "offline", 0)
    grade = read(row["grade"])
    if row["reward"] != 0 or row["exception"] or not grade.get("test_id_normalization"):
        raise RuntimeError("zero-model common verifier/collector did not pass")
    result = {"passed": True, "paid_model_calls": 0, "task": entry["task"],
              "result": row["result"], "result_sha256": row["result_sha256"],
              "grade": row["grade"], "grade_sha256": row["grade_sha256"]}
    atomic(root / "preparation/zero-model-plumbing.json", result)
    return result


def finish(session, tests_xml):
    session = Path(session)
    manifest = read(session / "manifest.json")
    import xml.etree.ElementTree as ET
    report = ET.parse(tests_xml).getroot()
    suites = list(report.iter("testsuite"))
    failures = sum(int(s.attrib.get("failures", 0)) + int(s.attrib.get("errors", 0)) for s in suites)
    tests = sum(int(s.attrib.get("tests", 0)) for s in suites)
    if failures or not tests:
        raise RuntimeError("offline test suite did not pass")
    followup = RELEASE / "offline-final-targeted-tests.xml"
    if followup.exists():
        latest = list(ET.parse(followup).getroot().iter("testsuite"))
        if any(int(s.attrib.get("failures", 0)) + int(s.attrib.get("errors", 0)) for s in latest):
            raise RuntimeError("final changed-code checks did not pass")
        cases = list(report.iter("testcase")) + list(ET.parse(followup).getroot().iter("testcase"))
        tests = len({(case.attrib.get("classname"), case.attrib["name"]) for case in cases})
    probes = {}
    for chain in ("click", "coredns"):
        root = next(Path(i["root"]) for i in manifest["runs"] if i["chain"] == chain and i["method"] == "experience-notes")
        probes[chain] = read(root / "preparation/zero-model-plumbing.json")
        if not probes[chain]["passed"]:
            raise RuntimeError("zero-model probe not passed")
    protected = read(RELEASE / "protected-baseline-hashes.json")
    changed = [p for p, expected in protected.items() if sha(p) != expected]
    if changed:
        raise RuntimeError("frozen parent/paid sources changed: " + str(changed))
    for item in manifest["runs"]:
        root = Path(item["root"])
        evidence = inventory(root)
        atomic(root / "preparation/task-inventory.json", {"passed": True, "tasks": evidence})
        pins = source_pins(root)
        atomic(root / "native-source-pins.json", pins)
        atomic(root / "preparation/readiness.json", {"passed": True, "paid_model_calls": 0,
            "configuration_sha256": sha(root / "configuration.json"), "tests_passed": tests,
            "tests_xml_sha256": sha(tests_xml), "task_inventory_sha256": sha(root / "preparation/task-inventory.json"),
            "changed_code_followup_tests_sha256": sha(followup) if followup.exists() else None,
            "probe": probes[item["chain"]], "initial_learning_state_empty": True,
            "held": True, "time": time.time()})
        validate(root)
    result = {"passed": True, "state": "ready_held", "new_runs": 6, "tests_passed": tests,
              "paid_model_calls": 0, "paid_experiments_started": False,
              "protected_files_unchanged": len(protected), "budget": budget_snapshot(read(Path(manifest["runs"][0]["root"]) / "configuration.json")),
              "time": time.time()}
    atomic(session / "readiness.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser("finish")
    p.add_argument("--session", type=Path, default=SESSION)
    p.add_argument("--tests-xml", type=Path, required=True)
    args = parser.parse_args()
    print(probe(args.root) if args.command == "probe" else finish(args.session, args.tests_xml))
