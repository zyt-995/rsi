"""Rootless-safe SWE-Chain Docker copies without modifying upstream sources.

The single-UID Docker daemon cannot perform ``docker cp`` ownership changes.
Archive extraction inside the container avoids the unsupported chown. Host-side
extraction uses Python's data filter to reject paths outside the destination.
"""

from __future__ import annotations

import io
import posixpath
import shlex
import subprocess
import tarfile
from pathlib import Path


def _root_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def copy_to(name: str, local_path: str, container_path: str) -> None:
    contents = local_path.endswith("/.")
    source = Path(local_path[:-2] if contents else local_path)
    if not source.exists():
        raise FileNotFoundError(source)
    target = posixpath.normpath(container_path)
    target_dir = target if contents else posixpath.dirname(target)
    if not target_dir.startswith("/"):
        raise ValueError("container target must be absolute")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        if contents:
            for child in sorted(source.iterdir()):
                archive.add(child, arcname=child.name, filter=_root_owner)
        else:
            archive.add(source, arcname=posixpath.basename(target), filter=_root_owner)
    result = subprocess.run(
        ["docker", "exec", "-i", "-w", "/", name, "sh", "-c",
         f"mkdir -p {shlex.quote(target_dir)} && tar -C {shlex.quote(target_dir)} -xf -"],
        input=stream.getvalue(), capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"rootless copy to {name} failed (exit {result.returncode}): "
            f"{result.stderr[-1000:]!r} {result.stdout[-1000:]!r}"
        )


def copy_from(name: str, container_path: str, local_path: str) -> None:
    contents = container_path.endswith("/.")
    source = posixpath.normpath(container_path[:-2] if contents else container_path)
    if not source.startswith("/"):
        raise ValueError("container source must be absolute")
    target = Path(local_path)
    source_dir = source if contents else posixpath.dirname(source)
    item = "." if contents else posixpath.basename(source)
    result = subprocess.run(
        ["docker", "exec", "-w", "/", name, "tar", "-C", source_dir, "-cf", "-", item],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"rootless copy from {name} failed: {result.stderr[-1000:]!r}")
    destination = target if contents else target.parent
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")
    if not contents:
        extracted = destination / posixpath.basename(source)
        if extracted != target:
            extracted.replace(target)


def patch_upstream() -> None:
    """Install rootless copies and offline task containers for this process."""
    from generate.container import DockerContainer as GenerationContainer
    from validation.container import DockerContainer as ValidationContainer

    if getattr(GenerationContainer, "_rsi_rootless_patched", False):
        return

    generation_start = GenerationContainer.start.__func__
    validation_exec = ValidationContainer.exec

    @classmethod
    def start_generation(cls, image, name, extra_args=None):
        return generation_start(cls, image, name, ["--network", "none", *(extra_args or [])])

    @classmethod
    def start_validation(cls, image, prefix="swe-test"):
        import uuid

        name = f"{prefix}-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "run", "-d", "--network", "none", "--name", name,
             image, "tail", "-f", "/dev/null"],
            capture_output=True, text=True, check=True,
        )
        uid = subprocess.run(
            ["docker", "exec", name, "id", "-u"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        gid = subprocess.run(
            ["docker", "exec", name, "id", "-g"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return cls(name, uid=uid, gid=gid)

    def cp_to(self, local_path: str, container_path: str) -> None:
        copy_to(self.name, local_path, container_path)

    def cp_from(self, container_path: str, local_path: str) -> None:
        copy_from(self.name, container_path, local_path)

    def exec_validation(self, cmd: str, **kw):
        if cmd == "pip install -e /app/code --no-deps -q":
            cmd = "pip install -e /app/code --no-deps --no-build-isolation --no-index -q"
            result = validation_exec(self, cmd, **kw)
            if result.returncode:
                raise RuntimeError(
                    "offline editable reinstall failed: " + result.stderr[-1500:]
                )
            return result
        return validation_exec(self, cmd, **kw)

    for cls in (GenerationContainer, ValidationContainer):
        cls.cp_to = cp_to
        cls.cp_from = cp_from
    GenerationContainer.start = start_generation
    ValidationContainer.start = start_validation
    ValidationContainer.exec = exec_validation
    GenerationContainer._rsi_rootless_patched = True
