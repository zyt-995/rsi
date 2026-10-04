#!/usr/bin/env python3
"""Copy tagged images between Docker daemons without touching source data.

The source VFS daemon materializes each `docker save` under its data root, so
keep batches small and verify every destination image ID. The script is safe to
rerun: already-matching tags are skipped.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

GIB = 1024**3


def docker(binary: str, host: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "-H", host, *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def image_info(binary: str, host: str, name: str) -> tuple[str, int] | None:
    try:
        result = docker(binary, host, "image", "inspect", name, "--format", "{{.Id}} {{.Size}}")
        image_id, size = result.stdout.strip().split()
        return image_id, int(size)
    except (subprocess.CalledProcessError, ValueError):
        return None


def transfer(binary: str, old_host: str, new_host: str, names: list[str]) -> None:
    source = subprocess.Popen(
        [binary, "-H", old_host, "save", *names], stdout=subprocess.PIPE
    )
    try:
        target = subprocess.Popen(
            [binary, "-H", new_host, "load"],
            stdin=source.stdout,
            stdout=subprocess.DEVNULL,
        )
        assert source.stdout is not None
        source.stdout.close()
        target_rc = target.wait()
        source_rc = source.wait()
        if source_rc or target_rc:
            raise RuntimeError(f"docker save/load failed: source={source_rc} target={target_rc}")
    finally:
        if source.poll() is None:
            source.terminate()
            source.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-host", default="unix:///tmp/rsi-harness-docker/docker.sock")
    parser.add_argument("--new-host", default="unix:///tmp/rsi-harness-docker-overlay2/docker.sock")
    parser.add_argument("--docker-bin", default="/home/ytzhang/budget-rl/.deps/docker-static/docker")
    parser.add_argument("--batch-gib", type=int, default=12)
    args = parser.parse_args()
    binary = args.docker_bin
    old_root = Path(docker(binary, args.old_host, "info", "--format", "{{.DockerRootDir}}").stdout.strip())
    new_info = docker(binary, args.new_host, "info", "--format", "{{.Driver}} {{.DockerRootDir}}").stdout.split()
    if len(new_info) != 2 or new_info[0] != "overlay2" or old_root == Path(new_info[1]):
        parser.error("destination must be a distinct overlay2 Docker daemon")
    new_root = Path(new_info[1])
    names = sorted(set(docker(binary, args.old_host, "image", "ls", "--format", "{{.Repository}}:{{.Tag}}").stdout.splitlines()))
    pending = []
    for name in names:
        source = image_info(binary, args.old_host, name)
        if source is None:
            raise RuntimeError(f"source image disappeared: {name}")
        target = image_info(binary, args.new_host, name)
        if target is None or source[0] != target[0]:
            pending.append((name, source[0], source[1]))
    print(f"Source tags: {len(names)}; pending: {len(pending)}", flush=True)
    batches: list[list[tuple[str, str, int]]] = []
    batch: list[tuple[str, str, int]] = []
    batch_size = 0
    for item in pending:
        if batch and batch_size + item[2] > args.batch_gib * GIB:
            batches.append(batch)
            batch = []
            batch_size = 0
        batch.append(item)
        batch_size += item[2]
    if batch:
        batches.append(batch)
    for number, batch in enumerate(batches, 1):
        old_free = shutil.disk_usage(old_root).free
        new_free = shutil.disk_usage(new_root).free
        if old_free < 30 * GIB or new_free < 150 * GIB:
            raise RuntimeError(
                f"disk reserve reached: old={old_free / GIB:.1f} GiB new={new_free / GIB:.1f} GiB"
            )
        batch_names = [item[0] for item in batch]
        print(
            f"Batch {number}/{len(batches)}: {len(batch_names)} tags, "
            f"{sum(item[2] for item in batch) / GIB:.1f} GiB logical; "
            f"old free {old_free / GIB:.1f} GiB",
            flush=True,
        )
        transfer(binary, args.old_host, args.new_host, batch_names)
        for name, image_id, _size in batch:
            target = image_info(binary, args.new_host, name)
            if target is None or target[0] != image_id:
                raise RuntimeError(f"image ID mismatch after migration: {name}")
        print(f"Verified batch {number}/{len(batches)}", flush=True)
    print(f"Verified all {len(names)} source image tags", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"Migration stopped safely: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
