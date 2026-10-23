"""Download a pinned official image archive and load it into existing Docker."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import urllib.request

from .common import ROOT, atomic, docker, queue, read, sha


def ensure_image(entry, load=True):
    catalog = read(ROOT / "data/image-catalog/catalog.json")
    name = entry["image"].split(":")[0].removeprefix("sweb.eval.x86_64.")
    relative = "images/" + name + ".tar"
    meta = next(row for row in catalog["files"] if row["path"] == relative)
    expected = meta["lfs"]["oid"]
    cache = ROOT / "data/image-cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (name + ".tar")
    receipt = cache / (name + ".receipt.json")
    if load and receipt.exists():
        saved = read(receipt)
        inspected = docker("image", "inspect", entry["image"], "--format", "{{.Id}}", check=False)
        if inspected.returncode == 0 and inspected.stdout.decode().strip() == saved.get("docker_image_id"):
            if saved.get("archive_sha256") != expected:
                raise RuntimeError("loaded image provenance changed")
            return saved
    part = target.with_suffix(".tar.part")
    url = (os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/") + "/datasets/" + catalog["repository"] + "/resolve/"
           + catalog["revision"] + "/" + relative)
    if not target.exists():
        for attempt in range(6):
            start = part.stat().st_size if part.exists() else 0
            if start == meta["size"]:
                break
            headers = {"Range": f"bytes={start}-"} if start else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                    if start and response.status != 206:
                        start = 0
                    mode = "ab" if start else "wb"
                    size = start
                    last = time.monotonic()
                    with part.open(mode) as output:
                        while data := response.read(4 * 1024 * 1024):
                            output.write(data)
                            size += len(data)
                            if time.monotonic() - last > 10:
                                print(json.dumps({"image": name, "downloaded": size,
                                                  "total": meta["size"]}), flush=True)
                                last = time.monotonic()
                if part.stat().st_size == meta["size"]:
                    break
                raise RuntimeError("incomplete image download")
            except Exception as exc:
                if attempt == 5:
                    raise
                print(json.dumps({"image": name, "retry": attempt + 1,
                                  "error_type": type(exc).__name__}), flush=True)
                time.sleep(min(2 ** attempt, 10))
        if part.stat().st_size != meta["size"] or sha(part) != expected:
            raise RuntimeError("official image archive size/SHA256 mismatch")
        part.replace(target)
    if target.stat().st_size != meta["size"] or sha(target) != expected:
        raise RuntimeError("cached official image archive changed")
    saved = {"image": entry["image"], "archive": str(target),
             "archive_sha256": expected, "repository_revision": catalog["revision"],
             "archive_bytes": meta["size"]}
    if load:
        result = docker("load", "--input", target, timeout=600, check=False)
        if result.returncode:
            error = result.stderr.decode(errors="replace")
            if "Lchown" not in error or "invalid argument" not in error:
                raise RuntimeError("Docker image load failed: " + error[-3000:])
            from .image_compat import normalize_archive
            derived, provenance = normalize_archive(target)
            result = docker("load", "--input", derived, timeout=600, check=False)
            if result.returncode:
                raise RuntimeError("normalized image load failed: " + result.stderr.decode(errors="replace")[-3000:])
            saved["rootless_compatibility"] = provenance
        saved["docker_image_id"] = docker("image", "inspect", entry["image"],
                                         "--format", "{{.Id}}").stdout.decode().strip()
    atomic(receipt, saved)
    print(json.dumps(saved), flush=True)
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", default="development", choices=("development", "pilot", "main"))
    parser.add_argument("--indices", default="0")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    entries = queue(args.cohort)
    for index in map(int, args.indices.split(",")):
        ensure_image(entries[index], load=not args.download_only)


if __name__ == "__main__":
    main()
