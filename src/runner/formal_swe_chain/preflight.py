"""Exercise the actual formal result collector on an affected task, at zero API cost."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

from swe_chain_evo.common import ROOT, PROJECT, atomic, read, sha
from .trial import TrialRunner


def check(root):
    output = root / "formal-score-preflight"
    output.mkdir(exist_ok=True)
    entry = read(root / "entries/1.json")
    config = read(root / "configuration.json")
    broker = SimpleNamespace(config=config,
        scopes={"no-model-calls": {"pending_tokens": 0, "blocked": False, "calls": []}},
        ledger=SimpleNamespace(snapshot=lambda: {"blocking": []}))
    runner = TrialRunner(root, broker, config["runtime"]["gateway_port"])
    results = list((output / "trials").glob("*/result.json"))
    if not results:
        cmd = [str(PROJECT / ".venv-terminal/bin/harbor"), "trials", "start", "--path", entry["path"],
            "--trials-dir", str(output / "trials"), "--env", "swe_chain_evo.environment:OfflineDocker",
            "--agent-import-path", "swe_chain_evo.preflight:NoopAgent",
            "--verifier", "swe_chain_evo.verifier:SWEVerifier",
            "--verifier-kwarg", "entry_file=" + str(root / "entries/1.json"),
            "--verifier-kwarg", "entry_sha256=" + sha(root / "entries/1.json")]
        env = dict(runner.env)
        for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
            env.pop(key, None)
        env["PYTHONPATH"] = str(ROOT) + ":" + str(PROJECT)
        with (output / "worker.log").open("ab") as log:
            subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=1000)
    source = {"candidate_sha256": None, "modules_hashes": None,
              "composer_name": "llm_dynamic", "composer_scope": "all", "locked_module_type": None}
    atomic(output / "launch.json", {"command": [], "development_only": True, "model_calls": 0})
    summary = runner._collect("kira-static", 1, output, "formal", source,
                              {"preflight": True}, "no-model-calls", 0)
    result = Path(summary["result"])
    grade = read(summary["grade"])
    normalization = grade.get("test_id_normalization")
    passed = (summary["reward"] == 0 and not summary["exception"] and summary["usage_complete"]
              and not summary["call_ids"] and result.with_name("result.raw.json").exists()
              and normalization and Path(summary["grade"]).with_name("grade.raw.json").exists())
    value = {"passed": bool(passed), "model_calls": 0, "development_only": True,
        "task": entry["task"], "normalization": normalization,
        "raw_result_sha256": sha(result.with_name("result.raw.json")),
        "collected_result_sha256": summary["result_sha256"],
        "formal_collector_sha256": sha(ROOT / "formal_swe_chain/trial.py")}
    atomic(root / "formal-score-preflight.json", value)
    if not passed:
        raise RuntimeError("formal score normalization/collector preflight failed")
    return value
