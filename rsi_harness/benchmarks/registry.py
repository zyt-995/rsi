from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = PROJECT_ROOT / "benchmarks" / "lock.json"


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def docker_runtime() -> dict[str, Any]:
    """Locate a normal Docker install or this host's user-namespace daemon."""
    candidates = [
        os.environ.get("RSI_DOCKER_BIN"),
        shutil.which("docker"),
        str(Path.home() / "budget-rl" / ".deps" / "docker-static" / "docker"),
    ]
    executable = next((str(Path(p)) for p in candidates if p and Path(p).is_file()), None)
    if (PROJECT_ROOT / ".docker/active-overlay2").is_file():
        # A missing active socket is an error, never a reason to use old VFS.
        host = "unix:///tmp/rsi-harness-docker-overlay2/docker.sock"
    else:
        host_candidates = [
            os.environ.get("DOCKER_HOST"),
            "unix:///tmp/rsi-harness-docker/docker.sock",
            f"unix:///tmp/{Path.home().name}-docker-unshare/docker.sock",
            "unix:///var/run/docker.sock",
        ]
        host = next((h for h in host_candidates if h and (
            not h.startswith("unix://") or Path(h.removeprefix("unix://")).exists()
        )), None)
    result: dict[str, Any] = {"executable": executable, "host": host, "available": False}
    if executable:
        env = os.environ.copy()
        if host:
            env["DOCKER_HOST"] = host
        check = subprocess.run(
            [executable, "version", "--format", "{{.Server.Version}}"],
            text=True, capture_output=True, check=False, env=env,
        )
        result.update({"available": check.returncode == 0,
                       "server_version": check.stdout.strip() or None})
        if check.returncode:
            result["error"] = "docker daemon is unreachable"
        else:
            details = subprocess.run(
                [executable, "info", "--format", "{{.Driver}}|{{.DockerRootDir}}"],
                text=True, capture_output=True, check=False, env=env,
            )
            if details.returncode == 0 and "|" in details.stdout:
                result["storage_driver"], result["data_root"] = details.stdout.strip().split("|", 1)
    return result


def docker_subprocess_env() -> dict[str, str]:
    runtime = docker_runtime()
    if not runtime["available"]:
        raise RuntimeError(runtime.get("error", "docker is unavailable"))
    env = os.environ.copy()
    env["PATH"] = str(Path(runtime["executable"]).parent) + os.pathsep + env.get("PATH", "")
    if runtime["host"]:
        env["DOCKER_HOST"] = runtime["host"]
    return env


def preflight(path: Path = LOCK_PATH) -> dict[str, Any]:
    lock = load_lock(path)
    docker = docker_runtime()
    results: dict[str, Any] = {"schema_version": 2, "ready": True,
                              "smoke_ready": True, "docker": docker, "benchmarks": {}}
    for name, spec in lock["benchmarks"].items():
        checkout = PROJECT_ROOT / spec["path"]
        checks: dict[str, Any] = {
            "checkout_exists": checkout.is_dir(),
            "expected_commit": spec["commit"],
            "actual_commit": None,
            "commit_matches": False,
            "entrypoint_exists": True,
            "runtime_available": True,
            "reasons": [],
        }
        if checkout.is_dir():
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=checkout, text=True,
                capture_output=True, check=False,
            )
            if result.returncode == 0:
                checks["actual_commit"] = result.stdout.strip()
                checks["commit_matches"] = checks["actual_commit"] == spec["commit"]
            if not checks["commit_matches"]:
                checks["reasons"].append("checkout is not at the locked commit")
            dirty = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"], cwd=checkout,
                text=True, capture_output=True, check=False,
            )
            checks["tracked_local_changes"] = [
                line for line in dirty.stdout.splitlines() if line.strip()
            ]
        else:
            checks["reasons"].append("checkout is missing")
        entrypoint = spec["entrypoint"]
        if "/" in entrypoint or entrypoint.endswith(".py"):
            checks["entrypoint_exists"] = (checkout / entrypoint).exists()
            if not checks["entrypoint_exists"]:
                checks["reasons"].append("upstream entrypoint is missing")
        if spec["runtime"] == "docker":
            checks["runtime_available"] = docker["available"]
            if not checks["runtime_available"]:
                checks["reasons"].append("docker executable is unavailable")
        if name == "evoarena":
            dataset = checkout / "EvoMem-PersonaMem-Evo" / "data" / "personamem-evo-10p.csv"
            histories = checkout / "EvoMem-PersonaMem-Evo" / "data" / "chat_history_32k"
            checks["released_dataset"] = dataset.is_file()
            checks["history_files"] = len(list(histories.glob("*.json"))) if histories.is_dir() else 0
            checks["verified_smoke"] = (
                PROJECT_ROOT / "runs" / "personamem-codex-smoke-v2" / "summary.json"
            ).is_file()
            if not checks["released_dataset"]:
                checks["reasons"].append("released PersonaMem-Evo data is missing")
        elif name == "continual-skill-bench":
            checks["harbor_environment"] = (PROJECT_ROOT / ".venv-csb" / "bin" / "harbor").is_file()
            checks["compose_plugin"] = (PROJECT_ROOT / ".docker" / "cli-plugins" / "docker-compose").is_file()
            smoke_result = PROJECT_ROOT / "runs" / "csb-native" / "rsi-csb-offline-smoke" / "result.json"
            checks["verified_offline_smoke"] = smoke_result.is_file()
            if not checks["harbor_environment"]:
                checks["reasons"].append("project Harbor environment is missing")
            if not checks["compose_plugin"]:
                checks["reasons"].append("Docker Compose plugin is missing")
        elif name == "swe-chain":
            checks["released_chains"] = len(list((checkout / "data").glob("*_specs_chain.jsonl")))
            checks["oracle_files"] = len(list((checkout / "oracle").glob("*/*/*.json")))
            checks["python_environment"] = (PROJECT_ROOT / ".venv-swe" / "bin" / "python").is_file()
            checks["smoke_image"] = False
            if docker["available"]:
                env = docker_subprocess_env()
                image = subprocess.run(
                    [docker["executable"], "image", "inspect", "swe-chain:attrs-21.3.0"],
                    text=True, capture_output=True, check=False, env=env,
                )
                checks["smoke_image"] = image.returncode == 0
            if not checks["released_chains"] or not checks["oracle_files"]:
                checks["reasons"].append("released SWE-Chain data or oracle files are missing")
            if not checks["python_environment"]:
                checks["reasons"].append("SWE-Chain Python environment is missing")
            if not checks["smoke_image"]:
                checks["reasons"].append("SWE-Chain smoke image is missing")
        checks["ready"] = not checks["reasons"]
        results["benchmarks"][name] = checks
        results["ready"] = results["ready"] and checks["ready"]
        results["smoke_ready"] = results["smoke_ready"] and checks["ready"]
    # Smoke readiness means the runners, released data and representative images
    # work. Formal offline readiness additionally requires prebuilding every task
    # image and removing each verifier's runtime package downloads.
    results["formal_offline_ready"] = False
    results["formal_offline_blockers"] = [
        "all ContinualSkillBench task environments/verifier dependencies are not prebuilt",
        "all SWE-Chain per-version images are not prebuilt",
        "local-model-only egress enforcement has not yet been applied to native benchmark containers",
    ]
    return results
