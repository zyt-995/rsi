"""Grade a predicted patch in an isolated official evaluation image."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import time
import uuid

from .common import ROOT, atomic, docker, instance, queue, sha


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def copy_into(container, source, destination):
    """docker cp uses host ownership; a root-owned stream supports single UID Docker."""
    source = Path(source)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(Path(destination).name)
        info.size = source.stat().st_size
        info.mode = 0o600
        with source.open("rb") as stream:
            archive.addfile(info, stream)
    docker("exec", "--interactive", "--user", "0", container, "tar", "--no-same-owner",
           "-xf", "-", "-C", str(Path(destination).parent), input=buffer.getvalue())


def parse_statuses(text):
    statuses = {}
    priority = {"PASS": 0, "SKIP": 1, "FAIL": 2, "ERROR": 3}
    def add(name, status):
        old = statuses.get(name)
        if old is None or priority[status] > priority[old]:
            statuses[name] = status
    for line in ANSI.sub("", text).splitlines():
        line = line.strip()
        go = re.match(r"--- (PASS|FAIL|SKIP): (\S+) \(", line)
        if go:
            add(go[2], go[1])
            continue
        summary = re.match(r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS) (.+)$", line)
        if summary:
            name = re.split(r"\s+-\s+", summary[2], maxsplit=1)[0].strip()
            if "::" in name:
                add(name, {"PASSED": "PASS", "FAILED": "FAIL", "SKIPPED": "SKIP",
                           "ERROR": "ERROR", "XFAIL": "SKIP", "XPASS": "FAIL"}[summary[1]])
            continue
        verbose = re.match(r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)(?:\s|$)", line)
        if verbose:
            add(verbose[1], {"PASSED": "PASS", "FAILED": "FAIL", "SKIPPED": "SKIP",
                             "ERROR": "ERROR", "XFAIL": "SKIP", "XPASS": "FAIL"}[verbose[2]])
    return statuses


def result_from_log(item, output):
    statuses = parse_statuses(output)
    required = list(dict.fromkeys(item["fail_to_pass"] + item["pass_to_pass"]))
    missing = [name for name in required if name not in statuses]
    failed = [name for name in required if statuses.get(name) != "PASS"]
    return {"reward": int(not failed), "required_tests": len(required),
            "observed_required_tests": len(required) - len(missing),
            "passed_required_tests": len(required) - len(failed),
            "missing_tests": missing, "nonpassing_tests": failed,
            "test_statuses": statuses}


def test_paths(patch):
    paths = []
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            parts = shlex.split(line)
            for name in parts[2:4]:
                value = name[2:]
                if value != "/dev/null" and value not in paths:
                    if value.startswith("/") or ".." in Path(value).parts:
                        raise ValueError("invalid test patch path")
                    paths.append(value)
    return paths


def extract_prediction(container, output):
    output = Path(output)
    result = docker("exec", "--user", "0", "--workdir", "/testbed", container,
                    "bash", "-lc", "git add -N . && git diff --binary HEAD", check=False)
    if result.returncode:
        raise RuntimeError("could not extract prediction from solver workspace")
    if len(result.stdout) > 32 * 1024 * 1024:
        raise ValueError("predicted patch exceeds 32 MiB")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.stdout)
    return output


def grade(entry, prediction, output, timeout=600):
    from . import offline_go_dependencies as go_deps
    from . import private_go_test_readiness as readiness
    from . import go_test_isolation as isolation
    deadline = time.monotonic() + timeout
    item = instance(entry)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    prediction = Path(prediction).resolve()
    identity = {"instance_id": item["instance_id"],
                "instance_sha256": sha(ROOT / "data/SWE-Chain-Evo" / entry["instance_path"]),
                "prediction_sha256": sha(prediction), "grader_sha256": sha(Path(__file__)),
                "offline_go_dependency_resolver_sha256": sha(Path(go_deps.__file__)),
                "private_go_test_readiness_sha256": sha(Path(readiness.__file__)),
                "go_test_isolation_sha256": sha(Path(isolation.__file__)),
                "image_id": docker("image", "inspect", item["image"], "--format", "{{.Id}}").stdout.decode().strip()}
    report_path = output / "grade.json"
    if report_path.exists():
        saved = json.loads(report_path.read_text())
        if saved["identity"] != identity:
            raise RuntimeError("cached grade inputs changed")
        return saved
    container = "swe-evo-grader-" + uuid.uuid4().hex[:16]
    raw = item["raw"]
    (output / "test.patch").write_text(raw["test_patch"])
    atomic(output / "identity.json", identity)
    docker("run", "--detach", "--name", container, "--network", "none", "--user", "0",
           "--workdir", "/testbed", "--entrypoint", "/bin/bash", item["image"],
           "-c", "sleep infinity")
    try:
        copy_into(container, prediction, "/tmp/prediction.patch")
        copy_into(container, output / "test.patch", "/tmp/benchmark-tests.patch")
        resets = []
        for path in test_paths(raw["test_patch"]):
            q = shlex.quote(path)
            resets.append(f"git checkout {shlex.quote(raw['base_commit'])} -- {q} 2>/dev/null || rm -f -- {q}")
        setup = ("set -e\ncd /testbed\ngit config --global --add safe.directory /testbed\n"
                 + "git reset --hard " + shlex.quote(raw["base_commit"]) + "\n"
                 + "if [ -s /tmp/prediction.patch ]; then git apply --whitespace=nowarn /tmp/prediction.patch; fi\n"
                 + "\n".join(resets) + "\n"
                 + "if [ -s /tmp/benchmark-tests.patch ]; then git apply --whitespace=nowarn /tmp/benchmark-tests.patch; fi\n")
        setup_result = docker("exec", "--user", "0", container, "bash", "-lc", setup, check=False)
        (output / "setup.log").write_bytes(setup_result.stdout + setup_result.stderr)
        if setup_result.returncode:
            # A bad prediction can legitimately fail to apply. The official
            # reference/no-op preflight establishes that the base setup works.
            saved = {"identity": identity, "reward": 0, "reason": "prediction_or_test_patch_apply_failed",
                     "setup_exit_code": setup_result.returncode, "test_statuses": {}}
            atomic(report_path, saved)
            return saved
        isolation.apply(item, container, output, docker, copy_into, atomic, test_paths(raw["test_patch"]))
        readiness.apply(item, container, output, docker, copy_into, atomic)
        prefix = ("set +e\n"
                  "if [ -f /opt/miniconda3/etc/profile.d/conda.sh ]; then "
                  "source /opt/miniconda3/etc/profile.d/conda.sh; conda activate testbed; fi\n"
                  "cd /testbed\n")
        text = ""
        exits = []
        for number, command in enumerate(raw["test_cmds"]):
            script = prefix + "echo SWE_EVO_TEST_COMMAND_BEGIN\n" + command + "\n"
            for dependency_attempt in range(4):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("original verifier600s deadline expired")
                try:
                    result = docker("exec", "--user", "0", container, "bash", "-lc", script,
                                    timeout=remaining, check=False)
                    log = (result.stdout + result.stderr).decode(errors="replace")
                    exit_code = result.returncode
                except subprocess.TimeoutExpired as exc:
                    log = ((exc.stdout or b"") + (exc.stderr or b"")).decode(errors="replace")
                    exit_code = "timeout"
                missing = go_deps.missing_modules(log)
                if not missing:
                    break
                archive = output / "infrastructure-attempts"
                archive.mkdir(exist_ok=True)
                (archive / f"command-{number:02d}-attempt-{dependency_attempt}.log").write_text(log)
                if dependency_attempt == 3:
                    raise RuntimeError("private grader public dependencies remain unavailable; do not score as wrong answer")
                go_deps.hydrate(container, missing, output, deadline)
            exits.append(exit_code)
            (output / f"command-{number:02d}.log").write_text(log)
            if "SWE_EVO_TEST_COMMAND_BEGIN" not in log:
                raise RuntimeError("grader did not reach the benchmark test command")
            text += log + "\n"
        saved = {"identity": identity, **result_from_log(item, text), "command_exit_codes": exits}
        atomic(report_path, saved)
        return saved
    finally:
        docker("rm", "--force", container, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", default="development")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--kind", choices=("oracle", "noop"), required=True)
    args = parser.parse_args()
    entry = queue(args.cohort)[args.index]
    item = instance(entry)
    folder = ROOT / "runs/grader-preflight" / item["instance_id"] / args.kind
    folder.mkdir(parents=True, exist_ok=True)
    prediction = folder / "prediction.patch"
    prediction.write_text(item["raw"]["patch"] if args.kind == "oracle" else "")
    result = grade(entry, prediction, folder)
    print(json.dumps({k:result[k] for k in result if k != "test_statuses"}, indent=2))
    if result["reward"] != int(args.kind == "oracle"):
        raise SystemExit("grader reference/no-op preflight failed")


if __name__ == "__main__":
    main()
