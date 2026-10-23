"""A zero-model-call agent for checking Harbor workspace and grader plumbing."""
import argparse
import os
from pathlib import Path
import subprocess

from .common import ROOT, PROJECT, existing_imports, read, atomic, sha
existing_imports()
from harbor.agents.base import BaseAgent


class NoopAgent(BaseAgent):
    @staticmethod
    def name():
        return "swe-evo-infrastructure-preflight"

    def version(self):
        return "1"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        result = await environment.exec(command="cd /testbed && git status --porcelain && git log --oneline -2")
        if result.return_code:
            raise RuntimeError("prepared workspace is unavailable")
        (self.logs_dir / "workspace.txt").write_text(result.stdout or "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    from rsi_portable.paths import runner_environment
    env = runner_environment(8058)
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(PROJECT)])
    entry = read(root / "task-stream.json")[0]
    output = root / "harbor-preflight"
    output.mkdir(exist_ok=True)
    results = list((output / "trials").glob("*/result.json"))
    if not results:
        cmd = [str(PROJECT / ".venv-terminal/bin/harbor"), "trials", "start", "--path", entry["path"],
               "--trials-dir", str(output / "trials"), "--env", "swe_chain_evo.environment:OfflineDocker",
               "--agent-import-path", "swe_chain_evo.preflight:NoopAgent",
               "--verifier", "swe_chain_evo.verifier:SWEVerifier",
               "--verifier-kwarg", "entry_file=" + str(root / "entries/0.json"),
               "--verifier-kwarg", "entry_sha256=" + sha(root / "entries/0.json")]
        with (output / "worker.log").open("ab") as log:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        results = list((output / "trials").glob("*/result.json"))
    result = read(results[0]) if len(results) == 1 else {}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    workspace = results[0].parent / "agent/workspace.txt" if results else None
    public = workspace.read_text() if workspace and workspace.exists() else ""
    passed = reward == 0 and not result.get("exception_info") and "benchmark base snapshot" in public
    if passed and len([l for l in public.splitlines() if l.strip()]) != 1:
        passed = False  # synthetic history must be the only visible commit.
    value = {"passed": passed, "model_calls": 0, "reward": reward,
             "result": str(results[0]) if results else None,
             "exception": result.get("exception_info"), "synthetic_history_only": passed}
    atomic(root / "harbor-preflight.json", value)
    print(value)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
