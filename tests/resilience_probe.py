"""Fault injector for disposable no-model infrastructure trials ONLY."""
from pathlib import Path

from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_resilient.harbor import ResilientVerifier


class InjectedGraderFailure(ResilientVerifier):
    async def verify(self):
        response = await self.environment._run_docker_compose_command(['ps', '-q', 'main'], check=True)
        ids = response.stdout.strip().splitlines()
        assert len(ids) == 1
        atomic_json(Path(self.trial_paths.verifier_dir) / 'retained-container.json', {
            'container': ids[0], 'task': self.task.name,
            'image': self.safe['tasks'][self.task.name]['image_id']})
        raise RuntimeError('offline_grading_injected_for_infrastructure_acceptance')
