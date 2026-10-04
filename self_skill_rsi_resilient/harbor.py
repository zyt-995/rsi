"""Retain exact container identity before grading; never feed errors as rewards."""
import asyncio
from pathlib import Path
import shutil

from harbor.models.verifier.result import VerifierResult
from offline_verifier.harbor import OfflineVerifier
from rsi_harness.terminal_infra import atomic_json
from .grading import load_manifest, grade_container


class ResilientVerifier(OfflineVerifier):
    def __init__(self, *args, manifest, manifest_sha256, **kwargs):
        safe = load_manifest(manifest, manifest_sha256)
        super().__init__(*args, manifest=safe['resilient']['base_manifest'],
                         manifest_sha256=safe['resilient']['base_sha256'], **kwargs)
        self.safe = safe

    async def verify(self):
        response = await self.environment._run_docker_compose_command(['ps', '-q', 'main'], check=True)
        ids = response.stdout.strip().splitlines()
        if len(ids) != 1:
            raise RuntimeError('cannot_identify_exact_solver_container')
        output = Path(self.trial_paths.verifier_dir)
        atomic_json(output / 'retained-container.json', {'container': ids[0],
            'task': self.task.name, 'image': self.safe['tasks'][self.task.name]['image_id']})
        attempt = output / 'attempts/00'
        record = await asyncio.to_thread(grade_container, ids[0], self.task.name, self.safe, attempt)
        for p in attempt.iterdir():
            if p.is_file():
                shutil.copy2(p, output / p.name)
        return VerifierResult(rewards={'reward': record['reward']})
