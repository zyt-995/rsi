"""Rewrite layer ownership for the existing single-UID rootless daemon.

Regular-file payloads, paths, modes and links are copied without modification.
Both the original archive and the derivation record remain available.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile

from .common import atomic, sha


def normalize_archive(source):
    source = Path(source)
    work = source.parent / (source.stem + ".singleuid-layers")
    work.mkdir(exist_ok=True)
    result = source.with_suffix(".singleuid.tar")
    record_path = source.with_suffix(".singleuid.json")
    if result.exists() and record_path.exists():
        record = json.loads(record_path.read_text())
        if (record["source_sha256"] == sha(source) and record["result_sha256"] == sha(result)
                and record.get("transform_source_sha256") == sha(Path(__file__))):
            return result, record
    with tarfile.open(source) as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("expected one official image per archive")
        item = manifest[0]
        config = json.load(archive.extractfile(item["Config"]))
        normalized = []
        ownership_changes = 0
        for index, name in enumerate(item["Layers"]):
            target = work / f"layer-{index:03d}.tar"
            source_stream = archive.extractfile(name)
            with tarfile.open(fileobj=source_stream, mode="r|*") as incoming, \
                 tarfile.open(target, mode="w", format=tarfile.PAX_FORMAT) as outgoing:
                for member in incoming:
                    changed = copy.copy(member)
                    changed.pax_headers = dict(member.pax_headers)
                    if member.uid != 0 or member.gid != 0:
                        ownership_changes += 1
                    changed.uid = changed.gid = 0
                    changed.uname = changed.gname = "root"
                    for key in ("uid", "gid", "uname", "gname"):
                        changed.pax_headers.pop(key, None)
                    outgoing.addfile(changed, incoming.extractfile(member) if member.isfile() else None)
            normalized.append((target, sha(target)))
            print(json.dumps({"normalizing_layer": index + 1, "layers": len(item["Layers"])}), flush=True)
        config["rootfs"]["diff_ids"] = ["sha256:" + digest for _, digest in normalized]
        labels = config.setdefault("config", {}).setdefault("Labels", {})
        labels["swe_chain_evo.single_uid_normalized"] = "true"
        labels["swe_chain_evo.source_archive_sha256"] = sha(source)
        payload = json.dumps(config, separators=(",", ":")).encode()
        config_name = hashlib.sha256(payload).hexdigest() + ".json"
        new_manifest = [{"Config": config_name, "RepoTags": item["RepoTags"],
                         "Layers": [f"layers/{digest}.tar" for _, digest in normalized]}]
        with tarfile.open(result, "w") as output:
            for name, data in [(config_name, payload), ("manifest.json", json.dumps(new_manifest).encode())]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
            for layer, digest in normalized:
                def root_owner(info):
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.pax_headers = {k:v for k,v in info.pax_headers.items()
                                        if k not in ("uid", "gid", "uname", "gname")}
                    return info
                output.add(layer, arcname=f"layers/{digest}.tar", recursive=False, filter=root_owner)
        record = {"source_sha256": sha(source), "result_sha256": sha(result),
                  "source_config": item["Config"], "derived_config": config_name,
                  "ownership_entries_changed": ownership_changes,
                  "transformation": "UID/GID set to 0; payloads, paths, modes and links preserved",
                  "transform_source_sha256": sha(Path(__file__))}
        atomic(record_path, record)
    for layer, _ in normalized:
        layer.unlink()
    work.rmdir()
    return result, record
