from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from .types import Action


def write_json(path: Path, data: object) -> None:
    """Atomic replacement within one filesystem; readers never see partial JSON."""
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateStore:
    """Immutable JSON revisions plus an atomic HEAD; all writes go through the host.

    Artifact names are logical paths inside the snapshots. No model-produced file
    or Python code is executed. This API boundary is not an OS security sandbox.
    """

    def __init__(self, root: Path, max_bytes: int = 65536):
        initial = {"MEMORY.md": "", "AGENTS.md": ""}
        if type(max_bytes) is not int or max_bytes < len(json.dumps(initial).encode("utf-8")):
            raise ValueError("state byte limit must fit the initial empty artifacts")
        self.root = root
        self.max_bytes = max_bytes
        root.mkdir(parents=True, exist_ok=True)
        self.revisions = root / "revisions"
        self.revisions.mkdir(exist_ok=True)
        if not (root / "HEAD.json").exists():
            write_json(self.revisions / "000000.json", {"revision": 0, "artifacts": initial})
            write_json(root / "HEAD.json", {"revision": 0})
        self.revision = json.loads((root / "HEAD.json").read_text())["revision"]
        self.artifacts = self._load(self.revision)

    @staticmethod
    def validate_path(value: object) -> str:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ValueError("invalid artifact path")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("artifact path must be a normalized relative path")
        if value not in {"MEMORY.md", "AGENTS.md"}:
            if len(path.parts) < 2 or path.parts[0] not in {"skills", "scripts"}:
                raise ValueError("editable surfaces: MEMORY.md, AGENTS.md, skills/, scripts/")
        return value

    def _load(self, revision: int) -> dict[str, str]:
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid revision")
        file = self.revisions / f"{revision:06d}.json"
        if not file.is_file():
            raise ValueError("unknown revision")
        return json.loads(file.read_text(encoding="utf-8"))["artifacts"]

    def view(self) -> dict:
        return {"revision": self.revision, "artifacts": dict(self.artifacts)}

    def preview(self, action: Action) -> dict:
        before = self.artifacts
        after = dict(before)
        if action.type == "WRITE_ARTIFACT":
            path = self.validate_path(action.payload.get("path"))
            content = action.payload.get("content")
            if not isinstance(content, str):
                raise ValueError("artifact content must be a string")
            after[path] = content
        elif action.type == "DELETE_ARTIFACT":
            path = self.validate_path(action.payload.get("path"))
            if path not in after:
                raise ValueError("artifact does not exist")
            del after[path]
        elif action.type == "ROLLBACK":
            target = action.payload.get("revision")
            if type(target) is not int or target > self.revision:
                raise ValueError("rollback must target an existing past revision")
            after = self._load(target)
        else:
            raise ValueError("unsupported state action")
        serialized = json.dumps(after, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(serialized) > self.max_bytes:
            raise ValueError("persistent state exceeds byte limit")
        changes = []
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) != after.get(path):
                changes.append({
                    "path": path,
                    "before": before.get(path),
                    "after": after.get(path),
                    "diff": "".join(difflib.unified_diff(
                        before.get(path, "").splitlines(keepends=True),
                        after.get(path, "").splitlines(keepends=True),
                        fromfile=f"before/{path}", tofile=f"after/{path}",
                    )),
                })
        return {
            "revision": self.revision,
            "artifacts": after,
            "changes": changes,
            "state_sha256": hashlib.sha256(serialized).hexdigest(),
        }

    def apply(self, action: Action, validation: dict | None = None) -> dict:
        preview = self.preview(action)
        before = self.artifacts
        after = preview["artifacts"]
        changes = preview["changes"]
        previous = self.revision
        if changes:
            # Skip a possible orphan left between snapshot creation and HEAD commit.
            next_revision = max(int(p.stem) for p in self.revisions.glob("*.json")) + 1
            write_json(self.revisions / f"{next_revision:06d}.json", {
                "revision": next_revision, "parent": previous, "action": action.to_dict(),
                "artifacts": after, "changes": changes, "validation": validation,
            })
            write_json(self.root / "HEAD.json", {"revision": next_revision})
            self.revision, self.artifacts = next_revision, after
        return {
            "previous_revision": previous, "revision": self.revision,
            "changed": bool(changes), "changes": changes,
            "state_sha256": preview["state_sha256"], "validation": validation,
        }
