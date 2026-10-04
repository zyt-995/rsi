"""Shared, fail-closed infrastructure checks for the Terminal campaign."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TERMINAL = PROJECT / "benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo"
DATASET = TERMINAL / "Terminal-Bench-Evo"
PORTS = (8040, 8041, 8042, 8043, 8044)
VARIANTS = {
    "terminus2_baseline": "baseline",
    "terminus2_evomem": "fixed-loop",
    "terminus2_self_action": "self-action",
    "terminus2_open_ended": "open-ended",
}
_hash_cache = {}


def docker_disk_status(project_root: Path) -> dict:
    """Gate new trials on the active Docker data filesystem, not a stale path."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode or not result.stdout.strip().startswith("/"):
            return {"ok": False, "error": "Docker data root unavailable"}
        docker_root = Path(result.stdout.strip())
        docker_free = shutil.disk_usage(docker_root).free
        project_free = shutil.disk_usage(project_root).free
        # Model-service logs and image-normalization staging still use /data1.
        data1_free = shutil.disk_usage("/data1").free
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    gib = 1024**3
    return {
        "ok": docker_free > 100 * gib
        and project_free > 5 * gib
        and data1_free > 10 * gib,
        "docker_root": str(docker_root),
        "docker_free_gib": round(docker_free / gib, 2),
        "project_free_gib": round(project_free / gib, 2),
        "data1_free_gib": round(data1_free / gib, 2),
    }


def file_hash(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    if key not in _hash_cache:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        _hash_cache[key] = digest.hexdigest()
    return _hash_cache[key]


def fingerprint_paths(task: Path) -> list[Path]:
    """Inputs whose content determines a preflight ticket's validity."""
    paths = [task / "task.toml"]
    for directory in (task / "environment", task / "tests"):
        paths.extend(
            p
            for p in directory.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.name != ".harbor-docker-build.lock"
        )
    paths += [
        PROJECT / ".cache/tmux" / name
        for name in (
            "tmux-runtime-singleuid-amd64.tar.gz",
            "verifier-runtime-singleuid-amd64.tar.gz",
        )
    ]
    paths += [
        PROJECT / "rsi_harness/terminal_smoke_agent.py",
        PROJECT / "scripts/preflight-terminal-environments.py",
        PROJECT / "scripts/runc-rootless.sh",
    ]
    harbor = TERMINAL / "harbor/src/harbor"
    paths += [
        harbor / p
        for p in (
            "verifier/verifier.py",
            "agents/terminus_2/tmux_session.py",
            "environments/docker/docker.py",
            "environments/docker/docker-compose-build.yaml",
        )
    ]
    return paths


def fingerprint(task: Path) -> str:
    """Invalidate readiness when task, runtime bundle, or relevant code changes."""
    payload = [
        (str(p.relative_to(PROJECT)), file_hash(p))
        for p in sorted(fingerprint_paths(task))
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def readiness(root: Path, task: str) -> dict:
    try:
        record = json.loads((root / "_infra/readiness" / f"{task}.json").read_text())
        if record.get("fingerprint") != fingerprint(DATASET / task):
            return {"status": "stale", "task": task}
        return record
    except (OSError, ValueError):
        return {"status": "missing", "task": task}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    temp.replace(path)


def flag_value(command: list[str], flag: str) -> str | None:
    try:
        return command[command.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def live_chain_workers(root: Path, proc_root: Path = Path("/proc")) -> list[tuple]:
    """Discover only our exact runner, owner, output path, variant and endpoint."""
    found = []
    runner = str(TERMINAL / "harbor_EvoMem/scripts/run_chain.py")
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            command = proc.joinpath("cmdline").read_bytes().decode().split("\0")
            if runner not in command:
                continue
            chain = flag_value(command, "--chain")
            arm = VARIANTS.get(flag_value(command, "--variant"))
            output = flag_value(command, "--trials-dir")
            if not chain or not arm or output != str(root / chain / arm):
                continue
            if Path(chain).name != chain:
                continue
            environment = proc.joinpath("environ").read_bytes().decode().split("\0")
            for port in PORTS:
                env_file = TERMINAL / f"harbor_EvoMem/scripts/terminus2_llm_{port}.env"
                if f"HARBOR_EVOMEM_LLM_ENV={env_file}" in environment:
                    found.append((str(port), chain, arm, proc.name))
        except (OSError, UnicodeError):
            continue
    return found
