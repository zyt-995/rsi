from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from rsi_portable.paths import WORKSPACE as ROOT, RUNTIME as PROJECT
PYTHON = PROJECT / ".venv-terminal/bin/python"


def read(path):
    return json.loads(Path(path).read_text())


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    os.replace(temporary, path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def docker_env():
    from rsi_portable.paths import docker_env as portable_docker_env
    return portable_docker_env()


def docker(*args, timeout=120, check=True, input=None):
    return subprocess.run(["docker", *map(str, args)], env=docker_env(),
                          input=input, capture_output=True, timeout=timeout,
                          check=check)


def queue(cohort="development"):
    return read(ROOT / "plans/task-order.json")["queues"][cohort]


def instance(entry):
    return read(ROOT / "data/SWE-Chain-Evo" / entry["instance_path"])


def existing_imports():
    # Billing uses provider receipts. Loading the bundled, pinned LiteLLM cost
    # map avoids an unrelated external fetch during every solver/editor start.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    for path in (PROJECT, PROJECT / "methods/vendor/meta-harness/reference_examples/terminal_bench_2"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
