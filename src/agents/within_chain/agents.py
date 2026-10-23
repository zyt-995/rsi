"""Fixed KIRA with an immutable experience note supplied by the coordinator."""
import os
from pathlib import Path

from swe_chain_evo.agents import KiraAudited
from swe_chain_evo.common import atomic, sha


def append_note(instruction, text):
    if not text:
        return instruction
    return (instruction + "\n\nExperience from previously completed milestones in this chain:\n"
            "Use relevant experience as guidance; check it against the current repository and task.\n"
            "<chain_experience>\n" + text + "\n</chain_experience>\n")


class KiraNotes(KiraAudited):
    async def run(self, instruction, environment, context):
        path = Path(os.environ["WITHIN_CHAIN_NOTE"])
        expected = os.environ["WITHIN_CHAIN_NOTE_SHA256"]
        if path.is_symlink() or sha(path) != expected:
            raise RuntimeError("immutable task note changed")
        text = path.read_text()
        atomic(self.logs_dir / "note-injection.json", {
            "note_sha256": expected, "note_text": text, "nonempty": bool(text),
            "available_in_task_instruction": True, "internal_use_is_not_observable": True})
        return await super().run(append_note(instruction, text), environment, context)
