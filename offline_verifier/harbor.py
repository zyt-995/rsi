"""Opt-in Harbor verifier plugin, without changing frozen upstream code."""
import asyncio
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.verifier import Verifier

from .runner import grade_container, load_manifest


class OfflineVerifier(Verifier):
    def __init__(self, *args, manifest, manifest_sha256, **kwargs):
        super().__init__(*args, **kwargs)
        if not isinstance(self.environment, DockerEnvironment) or self.step_name is not None:
            raise ValueError('offline_verifier_requires_single_step_local_docker_task')
        self.offline = load_manifest(manifest, manifest_sha256)
        task = self.task.name
        cfg = self.offline['tasks'].get(task)
        if cfg is None or self.task.checksum != cfg['task_checksum']:
            raise ValueError('task_not_registered_for_offline_grading')

    async def verify(self):
        response = await self.environment._run_docker_compose_command(['ps', '-q', 'main'], check=True)
        ids = response.stdout.strip().splitlines()
        if len(ids) != 1:
            raise RuntimeError('cannot_identify_exact_solver_container')
        record = await asyncio.to_thread(grade_container, ids[0], self.task.name, self.offline,
                                         Path(self.trial_paths.verifier_dir))
        return VerifierResult(rewards={'reward': record['reward']})
