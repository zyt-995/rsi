"""Audited resource-only handoff; never interrupt or restart a task worker.

The frozen solver/updater source, cohort, solve budgets and chain state stay intact.
Only the resource fields in the protocol are amended, preserving the original seal.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from rsi_harness import meta_skill_campaign as campaign
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash

ALLOWED_PORTS = {8040, 8041, 8042, 8044}


def resource_manifest(manifest, ports):
    if len(ports) != len(set(ports)) or not ports or set(ports) - ALLOWED_PORTS:
        raise ValueError("invalid resource ports")
    if not set(manifest["ports"]) <= set(ports):
        raise ValueError("this handoff supports expansion only")
    revised = copy.deepcopy(manifest)
    revised["ports"] = list(ports)
    revised["concurrency"] = len(ports)
    assert {k: v for k, v in manifest.items() if k not in ("ports", "concurrency")} == {
        k: v for k, v in revised.items() if k not in ("ports", "concurrency")}
    return revised


def check_environments(ports):
    base = campaign.DATASET.parent / "harbor_EvoMem/scripts"
    reference = dict(dotenv_values(base / "terminus2_llm_8040.env"))
    result = {}
    for port in ports:
        path = base / f"terminus2_llm_{port}.env"
        values = dict(dotenv_values(path))
        expected = {**reference, "LLM_BASE_URL": f"http://127.0.0.1:{port}/v1"}
        if values != expected:
            # Never print values: the env includes credentials.
            raise RuntimeError(f"endpoint {port} environment differs beyond base URL")
        if not campaign.core.endpoint_ok(port):
            raise RuntimeError(f"endpoint {port} is not serving the frozen model")
        result[str(port)] = {"path": str(path), "sha256": file_hash(path),
                             "compatibility": "exact match except base URL"}
    return result


def proc_args(pid):
    return Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]


def owned_supervisor(root, launch):
    pid = launch["pid"]
    if not campaign.alive(launch) or Path(f"/proc/{pid}").stat().st_uid != os.getuid():
        raise RuntimeError("supervisor identity or owner mismatch")
    args = proc_args(pid)
    expected = ["-u", "-m", "rsi_harness.meta_skill_campaign", "--root", str(root), "--run"]
    if args[1:] != expected:
        raise RuntimeError("refusing to signal an unrecognized supervisor")
    return pid


def live_stage_pids(root):
    result = set()
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            args = proc_args(int(path.name))
            if ("rsi_harness.meta_skill_campaign" in args and "--stage" in args
                    and "--root" in args and args[args.index("--root") + 1] == str(root)
                    and campaign.core.process_identity(int(path.name))):
                result.add(int(path.name))
        except (OSError, ValueError, IndexError):
            continue
    return result


def start_supervisor(root):
    with (root / "supervisor.log").open("ab", buffering=0) as stream:
        process = subprocess.Popen([sys.executable, "-u", "-m", "rsi_harness.meta_skill_campaign",
                                    "--root", str(root), "--run"], cwd=campaign.PROJECT,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    value = {"pid": process.pid, "identity": campaign.core.process_identity(process.pid),
             "started": time.time(), "root": str(root)}
    atomic_json(root / "supervisor_launch.json", value)
    return value


def resize(root, ports, reason):
    with campaign.exclusive(root / "resource-change.lock"):
        original = campaign.verify(root)
        revised = resource_manifest(original, ports)
        if revised == original:
            return {"status": "unchanged", "ports": ports}
        if (root / "PAUSE").exists():
            raise RuntimeError("campaign is paused")
        environments = check_environments(ports)
        old_active = read_json(campaign.CONTROL / "active.json", {})
        if any(campaign.alive(w) for p, w in old_active.items() if int(p) in ports):
            raise RuntimeError("historical work occupies requested resources")
        for port in set(ports) - set(original["ports"]):
            if not campaign.endpoint_idle(port):
                raise RuntimeError(f"new endpoint {port} already has work")
        disk = campaign.core.docker_disk_status(campaign.PROJECT)
        if not disk["ok"]:
            raise RuntimeError("disk admission failed")
        launch = read_json(root / "supervisor_launch.json")
        pid = owned_supervisor(root, launch)
        old_seal = read_json(root / "seal.json")
        audit = root / "resource_changes" / str(time.time_ns())
        audit.mkdir(parents=True)
        atomic_json(audit / "manifest.before.json", original)
        atomic_json(audit / "seal.before.json", old_seal)
        atomic_json(audit / "supervisor.before.json", launch)
        atomic_json(audit / "request.json", {"reason": reason, "time": time.time(),
                    "ports_before": original["ports"], "ports_after": ports,
                    "environments": environments, "disk": disk})
        stopped, retired, written = False, False, False
        try:
            # Freeze only the dispatcher while checking that all launched workers are
            # durably tracked. Workers/Harbor/model services continue uninterrupted.
            os.kill(pid, signal.SIGSTOP)
            stopped = True
            for _ in range(100):
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if state in ("T", "t"):
                    break
                time.sleep(.02)
            else:
                raise RuntimeError("dispatcher did not suspend")
            active = read_json(root / "active.json", {})
            if live_stage_pids(root) - {w["pid"] for w in active.values()}:
                raise RuntimeError("dispatch was between launch and checkpoint; resume and try later")
            completed = {str(p): file_hash(p) for p in root.glob("*/meta-skill-rsi/stages/*/record.json")
                         if read_json(p).get("status") == "complete"}
            atomic_json(audit / "active.before.json", active)
            atomic_json(audit / "completed.before.json", completed)
            atomic_json(audit / "status.before.json", read_json(root / "status.json", {}))
            # There is no running dispatcher in the brief manifest/seal update window.
            # Existing workers already verified their protocol at task admission.
            written = True
            atomic_json(root / "manifest.json", revised)
            new_seal = {**old_seal, "manifest_sha256": file_hash(root / "manifest.json"),
                        "resource_amendment": str(audit),
                        "previous_seal_sha256": file_hash(audit / "seal.before.json")}
            atomic_json(root / "seal.json", new_seal)
            campaign.verify(root)
            atomic_json(audit / "manifest.after.json", revised)
            atomic_json(audit / "seal.after.json", new_seal)
            # Retire the stopped, identified dispatcher only. Its task workers were
            # launched in separate sessions and the successor adopts their identities.
            owned_supervisor(root, launch)
            os.kill(pid, signal.SIGKILL)
            retired = True
            for _ in range(100):
                if not campaign.alive(launch):
                    break
                time.sleep(.02)
            else:
                raise RuntimeError("old dispatcher did not exit")
            successor = start_supervisor(root)
            for path, expected in completed.items():
                if file_hash(Path(path)) != expected:
                    raise RuntimeError("completed result changed during handoff")
            report = {"status": "handoff_started", "ports": ports, "concurrency": len(ports),
                      "supervisor": successor, "adopted": active,
                      "completed_results_preserved": len(completed),
                      "algorithm_source_unchanged": old_seal["code"] == new_seal["code"],
                      "audit": str(audit), "time": time.time()}
            atomic_json(audit / "result.json", report)
            return report
        except BaseException:
            if not retired:
                if written:
                    atomic_json(root / "manifest.json", original)
                    atomic_json(root / "seal.json", old_seal)
                if stopped and campaign.alive(launch):
                    os.kill(pid, signal.SIGCONT)
            else:
                # Preserve evidence/state for explicit recovery; do not blindly create
                # another supervisor if a successor could already have been started.
                atomic_json(audit / "needs_review.json", {"time": time.time(), "dispatcher_retired": True})
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ports", type=int, nargs="+", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    print(json.dumps(resize(args.root.resolve(), args.ports, args.reason), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
