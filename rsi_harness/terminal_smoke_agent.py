"""No model and no solution: exercise the formal agent's actual tmux setup."""

import json
from pathlib import PurePosixPath

from harbor.agents.nop import NopAgent
from harbor.agents.terminus_2.tmux_session import TmuxSession


class TerminalSmokeAgent(NopAgent):
    @staticmethod
    def name() -> str:
        return "rsi-infrastructure-smoke"

    async def setup(self, environment) -> None:
        session = TmuxSession(
            session_name="rsi-infrastructure-smoke",
            environment=environment,
            logging_path=PurePosixPath("/tmp/rsi-infrastructure-smoke.log"),
            local_asciinema_recording_path=None,
            remote_asciinema_recording_path=None,
        )
        await session.start()
        if not await session.is_session_alive():
            raise RuntimeError("tmux preflight did not start a live session")
        await session.stop()
        image = (
            environment.task_env_config.docker_image
            or f"hb__{environment.environment_id}:rsi-cache"
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "infrastructure.json").write_text(
            json.dumps(
                {
                    "tmux": "passed",
                    "image": image,
                    "environment_id": environment.environment_id,
                }
            )
        )
