from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .benchmarks.registry import docker_runtime, docker_subprocess_env
from .state import write_json
from .types import Action


BASELINE_PROGRAM = """\
import json, sys

request = json.load(sys.stdin)
json.dump({"delegate": {"instructions": "", "context": {}}}, sys.stdout)
"""


class ProgramExecutor(Protocol):
    def manifest(self) -> dict[str, Any]: ...

    def run(self, directory: Path, request: dict[str, Any]) -> dict[str, Any]: ...


class DockerProgramExecutor:
    """Run generated policy code without network or a writable host mount."""

    def __init__(self, image: str = "python:3.10-slim", timeout: float = 15):
        if not image or timeout <= 0:
            raise ValueError("image and a positive timeout are required")
        runtime = docker_runtime()
        if not runtime["available"]:
            raise RuntimeError("open-ended modes require a reachable Docker daemon")
        self.executable = runtime["executable"]
        self.image, self.timeout = image, timeout
        self._root: Path | None = None
        self._container: str | None = None

    def bind(self, root: Path) -> None:
        """Start one isolated worker per experiment; later decisions use docker exec."""
        if self._container:
            return
        self._root = root.resolve()
        name = f"rsi-program-{secrets.token_hex(6)}"
        command = [
            self.executable, "run", "--detach", "--name", name, "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "256m", "--cpus", "1",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "-v", f"{self._root}:/program:ro", self.image, "sleep", "infinity",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False,
                                env=docker_subprocess_env(), timeout=self.timeout)
        if result.returncode:
            raise RuntimeError("failed to start the generated-program sandbox")
        self._container = name

    def close(self) -> None:
        if self._container:
            subprocess.run([self.executable, "rm", "--force", self._container], text=True,
                           capture_output=True, check=False, env=docker_subprocess_env(),
                           timeout=self.timeout)
            self._container = None

    def manifest(self) -> dict[str, Any]:
        return {
            "executor": "docker", "image": self.image, "timeout_seconds": self.timeout,
            "network": "none", "read_only_root": True, "cap_drop": "ALL",
            "no_new_privileges": True,
        }

    def run(self, directory: Path, request: dict[str, Any]) -> dict[str, Any]:
        if not self._container or not self._root:
            raise RuntimeError("program executor is not bound to an experiment directory")
        try:
            relative = directory.resolve().relative_to(self._root)
        except ValueError as exc:
            raise ValueError("candidate is outside the mounted program store") from exc
        script = PurePosixPath("/program", *relative.parts, "agent.py").as_posix()
        module_root = PurePosixPath(script).parent.as_posix()
        bootstrap = (
            "import runpy,sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "runpy.run_path(sys.argv[2], run_name='__main__')"
        )
        command = [self.executable, "exec", "--interactive", self._container,
                   "python3", "-I", "-B", "-c", bootstrap, module_root, script]
        result = subprocess.run(
            command, input=json.dumps(request, ensure_ascii=False), text=True,
            encoding="utf-8", capture_output=True, timeout=self.timeout, check=False,
            env=docker_subprocess_env(),
        )
        if result.returncode:
            raise ValueError(f"candidate exited with code {result.returncode}")
        if len(result.stdout.encode("utf-8")) > 1_000_000:
            raise ValueError("candidate output exceeds 1 MB")
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("candidate did not emit one JSON object") from exc
        return self.validate_output(output)

    def validate_output(self, output: Any) -> dict[str, Any]:
        return validate_program_output(output)


class LocalProgramExecutor:
    """Trusted test fixture. Never use this for model-generated programs."""

    def __init__(self, timeout: float = 5):
        self.timeout = timeout

    def manifest(self) -> dict[str, Any]:
        return {"executor": "local-test-only", "timeout_seconds": self.timeout}

    def run(self, directory: Path, request: dict[str, Any]) -> dict[str, Any]:
        bootstrap = (
            "import runpy,sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "runpy.run_path(sys.argv[2], run_name='__main__')"
        )
        result = subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-I", "-B", "-c", bootstrap,
             str(directory), str(directory / "agent.py")],
            input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8",
            capture_output=True, timeout=self.timeout, check=False,
        )
        if result.returncode:
            raise ValueError(f"candidate exited with code {result.returncode}")
        try:
            return validate_program_output(json.loads(result.stdout))
        except json.JSONDecodeError as exc:
            raise ValueError("candidate did not emit one JSON object") from exc


def validate_program_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("candidate output must contain exactly action or delegate")
    if "action" in value:
        action = Action.from_dict(value["action"])
        return {"action": action.to_dict()}
    if "delegate" in value:
        delegate = value["delegate"]
        if not isinstance(delegate, dict) or set(delegate) - {"instructions", "context"}:
            raise ValueError("delegate may contain only instructions and context")
        instructions = delegate.get("instructions", "")
        context = delegate.get("context", {})
        if not isinstance(instructions, str) or not isinstance(context, dict):
            raise ValueError("invalid delegate output")
        if len(instructions.encode("utf-8")) > 32768 or len(json.dumps(context).encode()) > 65536:
            raise ValueError("delegate output exceeds its size limit")
        return {"delegate": {"instructions": instructions, "context": context}}
    raise ValueError("candidate output must contain action or delegate")


@dataclass(frozen=True)
class Candidate:
    id: str
    parent: int
    directory: Path
    label: str
    sha256: str


class ProgramStore:
    """Append-only executable agent versions plus an auditable candidate archive."""

    def __init__(self, root: Path, max_bytes: int = 131072):
        if type(max_bytes) is not int or max_bytes < len(BASELINE_PROGRAM):
            raise ValueError("program byte limit is too small")
        self.root, self.max_bytes = root, max_bytes
        self.versions = root / "versions"
        self.candidates = root / "candidates"
        self.versions.mkdir(parents=True, exist_ok=True)
        self.candidates.mkdir(exist_ok=True)
        if not (root / "HEAD.json").exists():
            baseline = self.versions / "000000"
            baseline.mkdir()
            (baseline / "agent.py").write_text(BASELINE_PROGRAM, encoding="utf-8")
            write_json(baseline / "metadata.json", {
                "revision": 0, "parent": None, "label": "fixed delegate baseline",
                "sha256": self._digest({"agent.py": BASELINE_PROGRAM}),
            })
            write_json(root / "HEAD.json", {"revision": 0})
        self.revision = json.loads((root / "HEAD.json").read_text())["revision"]

    @staticmethod
    def _validate_files(value: Any) -> dict[str, str]:
        if not isinstance(value, dict) or "agent.py" not in value or not value:
            raise ValueError("candidate files must include agent.py")
        files: dict[str, str] = {}
        for raw_path, content in value.items():
            if not isinstance(raw_path, str) or not isinstance(content, str):
                raise ValueError("candidate file names and contents must be strings")
            path = PurePosixPath(raw_path)
            if path.is_absolute() or any(p in {"", ".", ".."} for p in raw_path.split("/")):
                raise ValueError("candidate paths must be normalized relative paths")
            if path.suffix not in {".py", ".json", ".md", ".txt"}:
                raise ValueError("candidate file type is not allowed")
            if raw_path == "metadata.json":
                raise ValueError("metadata.json is reserved by the trusted host")
            files[raw_path] = content
        return files

    @staticmethod
    def _digest(files: dict[str, str]) -> str:
        payload = json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def active_directory(self) -> Path:
        return self.versions / f"{self.revision:06d}"

    def files(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.active_directory())): path.read_text(encoding="utf-8")
            for path in sorted(self.active_directory().rglob("*"))
            if path.is_file() and path.name != "metadata.json"
            and path.suffix in {".py", ".json", ".md", ".txt"}
        }

    def view(self, include_files: bool = False) -> dict[str, Any]:
        metadata = json.loads((self.active_directory() / "metadata.json").read_text())
        result = {"revision": self.revision, "label": metadata["label"], "sha256": metadata["sha256"]}
        if include_files:
            result["files"] = self.files()
            result["editable_surface"] = {
                "entrypoint": "agent.py",
                "allowed_suffixes": [".py", ".json", ".md", ".txt"],
                "replacement_semantics": "files replaces the complete active program",
            }
        return result

    def record_selection(self, candidate: Candidate, selected: bool, reason: str) -> None:
        metadata_path = candidate.directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["selection"] = {"selected": selected, "reason": reason}
        write_json(metadata_path, metadata)

    def stage(self, action: Action) -> Candidate:
        if action.type != "PROPOSE_AGENT":
            raise ValueError("unsupported program action")
        files = self._validate_files(action.payload.get("files"))
        if len(json.dumps(files, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
            raise ValueError("candidate exceeds program byte limit")
        label = action.payload.get("label", "")
        if not isinstance(label, str) or len(label) > 256:
            raise ValueError("candidate label must be a short string")
        digest = self._digest(files)
        candidate_id = f"c{len(list(self.candidates.iterdir())):06d}-{digest[:12]}"
        directory = self.candidates / candidate_id
        directory.mkdir()
        for name, content in files.items():
            destination = directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        write_json(directory / "metadata.json", {
            "id": candidate_id, "parent": self.revision, "label": label,
            "sha256": digest, "status": "staged",
        })
        return Candidate(candidate_id, self.revision, directory, label, digest)

    def record_validation(self, candidate: Candidate, result: dict[str, Any]) -> None:
        metadata_path = candidate.directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata.update({"status": "validated" if result.get("passed") else "rejected",
                         "validation": result})
        write_json(metadata_path, metadata)

    def activate(self, candidate: Candidate, validation: dict[str, Any]) -> dict[str, Any]:
        if candidate.parent != self.revision:
            raise ValueError("candidate parent is no longer active")
        next_revision = max(int(p.name) for p in self.versions.iterdir()) + 1
        destination = self.versions / f"{next_revision:06d}"
        destination.mkdir()
        for source in candidate.directory.rglob("*"):
            if source.is_file() and source.name != "metadata.json":
                relative = source.relative_to(candidate.directory)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        write_json(destination / "metadata.json", {
            "revision": next_revision, "parent": self.revision, "candidate": candidate.id,
            "label": candidate.label, "sha256": candidate.sha256, "validation": validation,
        })
        previous = self.revision
        write_json(self.root / "HEAD.json", {"revision": next_revision})
        self.revision = next_revision
        return {"changed": True, "previous_revision": previous, "revision": next_revision,
                "candidate": candidate.id, "validation": validation}

    def rollback(self, target: Any) -> dict[str, Any]:
        if type(target) is not int or target < 0 or target >= self.revision:
            raise ValueError("agent rollback must target an existing past revision")
        source_dir = self.versions / f"{target:06d}"
        if not source_dir.is_dir():
            raise ValueError("unknown program revision")
        previous = self.revision
        next_revision = max(int(p.name) for p in self.versions.iterdir()) + 1
        destination = self.versions / f"{next_revision:06d}"
        destination.mkdir()
        for source in source_dir.rglob("*"):
            if source.is_file() and source.name != "metadata.json":
                relative = source.relative_to(source_dir)
                output = destination / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(source.read_bytes())
        target_metadata = json.loads((source_dir / "metadata.json").read_text())
        write_json(destination / "metadata.json", {
            "revision": next_revision, "parent": previous, "rollback_target": target,
            "label": f"rollback to {target}", "sha256": target_metadata["sha256"],
        })
        write_json(self.root / "HEAD.json", {"revision": next_revision})
        self.revision = next_revision
        return {"changed": True, "previous_revision": previous, "revision": next_revision,
                "rollback_target": target}
