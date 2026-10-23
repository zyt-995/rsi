"""Zero-model Harbor checks of skill discovery, read-only mounts and the common grader."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from . import RELEASE, BACKBONE
from .skills import inventory, append_inventory
from swe_chain_evo.common import PROJECT, atomic, read, sha
from swe_chain_evo.preflight import NoopAgent


class SkillMountCheck(NoopAgent):
    async def run(self, instruction, environment, context):
        await super().run(instruction, environment, context)
        result = await environment.exec(command=(
            "cat /skills/adapter-check/SKILL.md && "
            "if touch /skills/adapter-check/forbidden 2>/dev/null; then exit 97; fi"))
        if result.return_code or "adapter-check" not in result.stdout:
            raise RuntimeError("skill mount content or read-only contract failed")
        (self.logs_dir / "skill-mount-check.txt").write_text(result.stdout)


def check(root):
    root = Path(root)
    out = root / "skill-mount-preflight"
    skills = out / "skills/adapter-check"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text("---\nname: adapter-check\ndescription: Offline adapter contract check.\n---\nNo model calls.\n")
    items, invalid = inventory(skills.parent)
    if invalid or "/skills/adapter-check/SKILL.md" not in append_inventory("task", items):
        raise ValueError("skill discovery failed")
    from rsi_portable.paths import runner_environment
    env = runner_environment(8064)
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join([str(RELEASE), str(BACKBONE), str(PROJECT)])
    entry = read(root / "task-stream.json")[0]
    cmd = [str(PROJECT / ".venv-terminal/bin/harbor"), "trials", "start", "--path", entry["path"],
        "--trials-dir", str(out / "trials"), "--env", "swe_chain_evo.environment:OfflineDocker",
        "--agent-import-path", "evoskill_swe.preflight:SkillMountCheck",
        "--verifier", "swe_chain_evo.verifier:SWEVerifier", "--mounts-json", json.dumps([
            {"type": "bind", "source": str(skills.parent), "target": "/skills", "read_only": True}]),
        "--verifier-kwarg", "entry_file=" + str(root / "entries/0.json"),
        "--verifier-kwarg", "entry_sha256=" + sha(root / "entries/0.json")]
    results = list((out / "trials").glob("*/result.json"))
    if not results:
        with (out / "worker.log").open("ab") as log:
            subprocess.run(cmd, env=env, cwd=RELEASE, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        results = list((out / "trials").glob("*/result.json"))
    result = read(results[0]) if len(results) == 1 else {}
    passed = (not result.get("exception_info") and len(results) == 1
              and (results[0].parent / "agent/skill-mount-check.txt").exists()
              and (result.get("verifier_result") or {}).get("rewards", {}).get("reward") == 0)
    value = {"passed": passed, "model_calls": 0, "read_only_mount_verified": passed,
             "common_verifier_used": True, "result": str(results[0]) if results else None,
             "exception": result.get("exception_info")}
    atomic(root / "skill-mount-preflight.json", value)
    print(value)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    check(parser.parse_args().root.resolve())
