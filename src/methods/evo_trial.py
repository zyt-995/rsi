"""One registered Evo task with the common model, grader, and trial budget.

Pilot trials can target any index. A formal campaign must use the frozen stream
in order and give an updater only rows whose immutable formal result exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from methods.comparison import DEFAULT_CONTRACT, check_contract, read, registered_grader
from rsi_portable.paths import runner_environment

from rsi_portable.paths import RUNTIME as PROJECT
META_PATH = PROJECT / "methods/vendor/meta-harness/reference_examples/terminal_bench_2"
HARBOR = PROJECT / ".venv-terminal/bin/harbor"
AGENTS = {
    "meta-harness": "methods.meta_runtime:MetaAuditedAgent",
    "modularrsi": "methods.modular_runtime:ModularAuditedAgent",
}
PROXY = "http://10.0.2.2:17897"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def with_grader(contract: dict, index: int) -> dict:
    entry = dict(contract["stream"][index])
    if "grader_manifest" not in entry:
        root = Path(contract["source"]["root"])
        manifest = read(root / "manifest.json")
        if int(entry["uid"][1:]) < 112:
            entry.update(verifier_import="terminal_offline_recover.harbor:RecoveryVerifier",
                         grader_manifest=manifest["resilience_migration"]["grader"],
                         grader_sha256=manifest["resilience_migration"]["grader_sha256"])
        else:
            entry.update(registered_grader(manifest, entry))
    if sha(Path(entry["grader_manifest"])) != entry["grader_sha256"]:
        raise ValueError("grader_changed:" + entry["task"])
    return entry


def command(method: str, entry: dict, output: Path, modules: Path | None = None,
            agent_import_path: str | None = None, composer_name: str = "llm_dynamic") -> list[str]:
    if method not in AGENTS:
        raise ValueError(method)
    if "grader_manifest" not in entry:
        raise ValueError("pilot and formal runs require the common recovered grader")
    cmd = [str(HARBOR), "trials", "start", "--path", entry["path"],
           "--trials-dir", str(output / "trials"), "--agent-import-path", agent_import_path or AGENTS[method],
           "--model", "openai/Qwen/Qwen3.8-27B", "--agent-timeout", "7200",
           "--verifier-timeout", "600"]
    kw = {"record_terminal_session": False, "max_turns": 15,
          "suppress_max_turns_warning": True,
          "llm_call_kwargs": {"max_tokens": 32768, "timeout": 2700},
          "api_base": "http://127.0.0.1:8040/v1"}
    if method == "meta-harness":
        kw["max_output_retries"] = 1
    else:
        if modules is None:
            from methods.evo_agents import VENDOR_AGENTS
            modules = VENDOR_AGENTS / "terminus_2_modular/modules"
        kw.update(modules_root=str(modules.resolve()), composer_name=composer_name,
                  model_info={"max_input_tokens": 176000, "max_output_tokens": 32768,
                              "input_cost_per_token": 0.0, "output_cost_per_token": 0.0})
    for key, val in kw.items():
        cmd += ["--agent-kwarg", key + "=" + (val if isinstance(val, str) else json.dumps(val))]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        cmd += ["--ae", f"{key}={PROXY}", "--ve", f"{key}={PROXY}"]
    for key in ("NO_PROXY", "no_proxy"):
        cmd += ["--ae", f"{key}=127.0.0.1,localhost", "--ve", f"{key}=127.0.0.1,localhost"]
    for key, val in {"GIT_PAGER": "cat", "PAGER": "cat"}.items():
        cmd += ["--ae", f"{key}={val}"]
    for key, val in {"TAR_OPTIONS": "--no-same-owner",
                     "PIP_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple",
                     "UV_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple"}.items():
        cmd += ["--ve", f"{key}={val}"]
    cmd += ["--verifier", entry["verifier_import"],
            "--verifier-kwarg", "manifest=" + entry["grader_manifest"],
            "--verifier-kwarg", "manifest_sha256=" + entry["grader_sha256"],
            "--environment-kwarg", "keep_containers=true"]
    return cmd


def run(method: str, index: int, output: Path, port: int = 8040,
        modules: Path | None = None, agent_import_path: str | None = None,
        extra_pythonpath: Path | None = None, composer_name: str = "llm_dynamic") -> dict:
    contract = read(DEFAULT_CONTRACT)
    check_contract(contract)
    entry = with_grader(contract, index)
    from harbor.models.task.task import Task
    if Task(Path(entry["path"])).checksum != entry["task_checksum"]:
        raise ValueError("task_checksum_changed:" + entry["task"])
    output.mkdir(parents=True, exist_ok=True)
    results = list((output / "trials").glob("*/result.json"))
    if results:
        raise FileExistsError("formal/pilot result exists; never replay a trial: " + str(results[0]))
    launch = output / "launch.json"
    if launch.exists():
        raise FileExistsError("launch exists; interrupted attempt requires review: " + str(launch))
    cmd = command(method, entry, output, modules, agent_import_path, composer_name)
    cmd = [part.replace("127.0.0.1:8040", f"127.0.0.1:{port}") for part in cmd]
    env = runner_environment(port)
    inspected = subprocess.check_output(["docker", "image", "inspect", entry["image_id"],
                                         "--format", "{{.Id}}"], env=env, text=True,
                                        timeout=20).strip()
    if inspected != entry["image_id"]:
        raise ValueError("registered_image_changed:" + entry["task"])
    env["PYTHONPATH"] = os.pathsep.join([str(extra_pythonpath or ""), str(PROJECT),
                                         str(META_PATH), env.get("PYTHONPATH", "")])
    env["OPENAI_API_KEY"] = "EMPTY"
    launch.write_text(json.dumps({"method": method, "index": index,
                                  "entry": entry, "command": cmd,
                                  "time": time.time()}, indent=2) + "\n")
    with (output / "worker.log").open("wb") as log:
        proc = subprocess.run(cmd, cwd=PROJECT, env=env, stdout=log,
                              stderr=subprocess.STDOUT, timeout=8400)
    results = list((output / "trials").glob("*/result.json"))
    value = read(results[0]) if len(results) == 1 else None
    summary = {"method": method, "index": index, "task": entry["task"],
               "returncode": proc.returncode, "result": str(results[0]) if value else None,
               "reward": ((value.get("verifier_result") or {}).get("rewards") or {}).get("reward") if value else None,
               "input_tokens": (value.get("agent_result") or {}).get("n_input_tokens") if value else None,
               "output_tokens": (value.get("agent_result") or {}).get("n_output_tokens") if value else None,
               "exception": (value.get("exception_info") or {}).get("exception_type") if value else None,
               "time": time.time()}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--method", choices=AGENTS, required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, choices=(8040, 8041), default=8040)
    p.add_argument("--modules", type=Path)
    p.add_argument("--agent-import-path")
    p.add_argument("--extra-pythonpath", type=Path)
    p.add_argument("--composer-name", default="llm_dynamic")
    a = p.parse_args()
    print(json.dumps(run(a.method, a.index, a.output, a.port, a.modules,
                         a.agent_import_path, a.extra_pythonpath, a.composer_name), indent=2))


if __name__ == "__main__":
    main()
