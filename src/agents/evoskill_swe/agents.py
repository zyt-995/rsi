"""Identical audited KIRA loop, with optional reusable skill discovery."""
from pathlib import Path
from swe_chain_evo.agents import KiraAudited
from swe_chain_evo.common import atomic
from .skills import inventory, append_inventory


class KiraSkills(KiraAudited):
    def __init__(self, *args, skills_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._host_skills_dir = Path(skills_dir) if skills_dir else None

    async def run(self, instruction, environment, context):
        items, invalid = inventory(self._host_skills_dir) if self._host_skills_dir else ([], [])
        atomic(self.logs_dir / "skill-discovery.json", {
            "skills": items, "invalid_skills": invalid,
            "empty_skills_preserve_original_instruction": not items})
        if invalid:
            raise ValueError("undiscoverable task skills; refuse to run without the configured skill inventory")
        return await super().run(append_inventory(instruction, items), environment, context)
