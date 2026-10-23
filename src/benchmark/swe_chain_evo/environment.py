"""Harbor Docker adapter using actual Docker network isolation on this daemon."""
from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode


class OfflineDocker(DockerEnvironment):
    @staticmethod
    def _requires_egress_control(*, startup_network_policy, phase_network_policies):
        if any(p.network_mode != NetworkMode.NO_NETWORK
               for p in (startup_network_policy, *phase_network_policies)):
            raise ValueError("SWE solver and verifier phases must remain offline")
        return False

    @property
    def capabilities(self):
        return super().capabilities.model_copy(update={"disable_internet": True})

    @property
    def _docker_compose_paths(self):
        return [*super()._docker_compose_paths, Path(__file__).with_name("offline-compose.yaml")]

    async def start(self, force_build):
        await super().start(force_build)
        result = await self._run_docker_compose_command(["ps", "-q", "main"], check=True)
        from .common import docker
        inspected = docker("inspect", result.stdout.strip(), "--format", "{{.HostConfig.NetworkMode}}")
        if inspected.stdout.decode().strip() != "none":
            raise RuntimeError("Docker did not enforce the SWE offline policy")
