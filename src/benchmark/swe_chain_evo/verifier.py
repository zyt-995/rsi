"""Harbor verifier: keep hidden test execution outside the solver container."""
import asyncio
from pathlib import Path

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from .common import read, sha
from .grading import extract_prediction, grade


class SWEVerifier(BaseVerifier):
    def __init__(self, *args, entry_file, entry_sha256, **kwargs):
        super().__init__(*args, **kwargs)
        path = Path(entry_file)
        if sha(path) != entry_sha256:
            raise ValueError("frozen SWE entry changed")
        self.entry = read(path)

    async def verify(self):
        response = await self.environment._run_docker_compose_command(["ps", "-q", "main"], check=True)
        ids = response.stdout.strip().splitlines()
        if len(ids) != 1:
            raise RuntimeError("cannot identify exact SWE solver container")
        folder = Path(self.trial_paths.verifier_dir)
        folder.mkdir(parents=True, exist_ok=True)
        patch = await asyncio.to_thread(extract_prediction, ids[0], folder / "prediction.patch")
        result = await asyncio.to_thread(grade, self.entry, patch, folder / "grading")
        return VerifierResult(rewards={"reward": result["reward"]})
