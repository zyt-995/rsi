"""Build agent-visible Harbor tasks from the current official base snapshot."""
from __future__ import annotations

import json
from pathlib import Path
import shlex

from .assets import ensure_image
from .common import ROOT, atomic, docker, instance, sha


def prepare_task(entry):
    item = instance(entry)
    receipt = ensure_image(entry)
    folder = ROOT / "prepared/tasks" / item["instance_id"]
    context = folder / "environment"
    context.mkdir(parents=True, exist_ok=True)
    base = item["raw"]["base_commit"]
    clean_tag = "swe-evo-solver:" + item["instance_id"].replace("__", "-").lower()
    script = ("set -eu\ncd /testbed\n"
              "git config --global --add safe.directory /testbed\n"
              f"git reset --hard {shlex.quote(base)}\n"
              "git clean -fd\n"
              "rm -rf /testbed/.git\n"
              "rm -f /eval.sh /test.patch /patch.diff /root/setup_repo.sh /root/setup_env.sh /root/.bash_history\n"
              "git init -q\ngit add -A\n"
              "git -c user.name=Benchmark -c user.email=benchmark@local commit -qm 'benchmark base snapshot'\n")
    (context / "prepare.sh").write_text(script)
    dockerfile = (f"FROM {item['image']}\nUSER 0\nWORKDIR /testbed\n"
                  "COPY prepare.sh /tmp/swe-prepare.sh\n"
                  "RUN bash /tmp/swe-prepare.sh && rm /tmp/swe-prepare.sh\n"
                  'ENV PATH="/opt/miniconda3/envs/testbed/bin:${PATH}"\n'
                  "ENTRYPOINT []\nCMD [\"sleep\", \"infinity\"]\n")
    (context / "Dockerfile").write_text(dockerfile)
    fingerprint = {"instance_sha256": sha(ROOT / "data/SWE-Chain-Evo" / entry["instance_path"]),
                   "source_image_id": receipt["docker_image_id"],
                   "prepare_sha256": sha(context / "prepare.sh"),
                   "dockerfile_sha256": sha(context / "Dockerfile")}
    record_path = folder / "preparation.json"
    cached = json.loads(record_path.read_text()) if record_path.exists() else None
    inspected = docker("image", "inspect", clean_tag, "--format", "{{.Id}}", check=False)
    if not (cached and cached["fingerprint"] == fingerprint and inspected.returncode == 0
            and inspected.stdout.decode().strip() == cached["solver_image_id"]):
        result = docker("build", "--network", "none", "--tag", clean_tag, context,
                        timeout=600, check=False)
        (folder / "image-build.log").write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("solver image preparation failed; inspect " + str(folder / "image-build.log"))
        solver_id = docker("image", "inspect", clean_tag, "--format", "{{.Id}}").stdout.decode().strip()
        atomic(record_path, {"fingerprint": fingerprint, "solver_image_id": solver_id,
                             "base_commit": base, "source_receipt": receipt,
                             "adaptation": "fresh base snapshot, hidden evaluator/history removed; official grader runs separately"})
    else:
        solver_id = cached["solver_image_id"]
    instruction = ("Work on the current software-maintenance milestone in /testbed. "
                   "Inspect the current code, implement the requested behavior, and run available public checks. "
                   "The environment already has its dependencies; task network access is disabled. "
                   "Preserve unrelated behavior. Finish with the intended source changes in this workspace.\n\n"
                   + item["problem_statement"] + "\n")
    (folder / "instruction.md").write_text(instruction)
    config = ("version = \"1.0\"\n[agent]\ntimeout_sec = 7200.0\n"
              "[verifier]\ntimeout_sec = 660.0\n"
              f"[environment]\ndocker_image = {json.dumps(solver_id)}\n"
              'network_mode = "no-network"\nworkdir = "/testbed"\n'
              "cpus = 2\nmemory_mb = 8192\nstorage_mb = 10240\n")
    (folder / "task.toml").write_text(config)
    (folder / "tests").mkdir(exist_ok=True)
    (folder / "tests/test.sh").write_text("#!/bin/sh\n# Grading is provided by the host-side SWEVerifier.\nexit 1\n")
    return {**entry, "task": item["instance_id"], "path": str(folder),
            "image_id": solver_id, "base_commit": base,
            "instruction": item["problem_statement"]}
