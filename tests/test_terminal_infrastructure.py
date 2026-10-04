import importlib.util
import json
import time
from pathlib import Path

import pytest

from rsi_harness import terminal_infra as infra

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / name
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_worker_discovery_requires_exact_identity(tmp_path):
    root = tmp_path / "run"
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, output, env, variant in [
        (1, root / "demo/baseline", 8040, "terminus2_baseline"),
        (2, root / "demo/baseline-extra", 8040, "terminus2_baseline"),
        (3, root / "demo/baseline", 9999, "terminus2_baseline"),
        (4, root / "demo/baseline", 8040, "unknown"),
    ]:
        target = proc / str(pid)
        target.mkdir()
        command = [
            "python",
            str(infra.TERMINAL / "harbor_EvoMem/scripts/run_chain.py"),
            "--chain",
            "demo",
            "--variant",
            variant,
            "--trials-dir",
            str(output),
        ]
        (target / "cmdline").write_bytes("\0".join(command).encode())
        env_file = infra.TERMINAL / f"harbor_EvoMem/scripts/terminus2_llm_{env}.env"
        (target / "environ").write_bytes(f"HARBOR_EVOMEM_LLM_ENV={env_file}\0".encode())
    assert infra.live_chain_workers(root, proc) == [("8040", "demo", "baseline", "1")]


def test_readiness_is_invalidated_by_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(infra, "fingerprint", lambda task: "new")
    target = tmp_path / "_infra/readiness/demo.json"
    infra.atomic_json(target, {"status": "ready", "fingerprint": "old"})
    assert infra.readiness(tmp_path, "demo")["status"] == "stale"
    infra.atomic_json(target, {"status": "ready", "fingerprint": "new"})
    assert infra.readiness(tmp_path, "demo")["status"] == "ready"
    target.write_text("partial")
    assert infra.readiness(tmp_path, "demo")["status"] == "missing"


def test_disk_gate_uses_active_docker_root(tmp_path, monkeypatch):
    class DockerInfo:
        returncode = 0
        stdout = "/data2/ytzhang/rsi-harness-docker-overlay2\n"

    class Usage:
        def __init__(self, free):
            self.free = free

    gib = 1024**3
    monkeypatch.setattr(infra.subprocess, "run", lambda *a, **kw: DockerInfo())
    monkeypatch.setattr(
        infra.shutil,
        "disk_usage",
        lambda path: Usage(150 * gib if str(path).startswith("/data2") else 20 * gib),
    )
    status = infra.docker_disk_status(tmp_path)
    assert status["ok"]
    assert status["docker_root"] == DockerInfo.stdout.strip()


def test_disk_gate_fails_closed_without_docker(tmp_path, monkeypatch):
    class DockerInfo:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(infra.subprocess, "run", lambda *a, **kw: DockerInfo())
    assert not infra.docker_disk_status(tmp_path)["ok"]


def test_gate_requires_all_remaining_stages(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(
        scheduler, "work_progress", lambda *args: {"missing": ["base", "evo"]}
    )
    calls = []

    def ready(root, task):
        calls.append(task)
        return {
            "status": "ready" if task == "base" else "failed",
            "image": "img",
            "image_id": "id",
        }

    monkeypatch.setattr(scheduler, "readiness", ready)

    class Image:
        returncode = 0
        stdout = "id\n"

    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **kw: Image())
    assert not scheduler.next_image_ready(tmp_path, "demo", "baseline", True)
    assert calls == ["base", "evo"]


def test_gate_blocks_changed_image(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(scheduler, "work_progress", lambda *args: {"missing": ["base"]})
    monkeypatch.setattr(
        scheduler,
        "readiness",
        lambda *args: {"status": "ready", "image": "img", "image_id": "old"},
    )

    class Image:
        returncode = 0
        stdout = "new\n"

    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **kw: Image())
    assert not scheduler.next_image_ready(tmp_path, "demo", "baseline", True)


def test_complete_tasks_bypass_preflight(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(scheduler, "work_progress", lambda *args: {"missing": []})
    monkeypatch.setattr(
        scheduler, "readiness", lambda *args: pytest.fail("completed task revisited")
    )
    assert scheduler.next_image_ready(tmp_path, "demo", "baseline", True)


def test_paired_stage_gate_checks_only_current_task(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(
        scheduler, "work_progress", lambda *args: {"missing": ["base", "evo"]}
    )
    calls = []

    def ready(root, task):
        calls.append(task)
        return {"status": "ready", "image": "img", "image_id": "id"}

    monkeypatch.setattr(scheduler, "readiness", ready)

    class Image:
        returncode = 0
        stdout = "id\n"

    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **kw: Image())
    assert scheduler.next_image_ready(tmp_path, "demo", "baseline", True, True)
    assert calls == ["base"]


def test_paired_order_fills_same_task_and_skips_valid_results(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    arms = list(scheduler.ARMS)
    monkeypatch.setattr(
        scheduler, "chain_tasks", lambda chain: [chain, f"{chain}-EVO-1"]
    )
    completed = {
        ("alpha", "baseline"): {"alpha"},
        ("alpha", "fixed-loop"): set(),
        ("alpha", "self-action"): set(),
        ("alpha", "open-ended"): set(),
        ("beta", "baseline"): set(),
        ("beta", "fixed-loop"): set(),
        ("beta", "self-action"): set(),
        ("beta", "open-ended"): set(),
    }
    monkeypatch.setattr(
        scheduler,
        "completed_task_names",
        lambda path: completed[(path.parent.name, path.name)],
    )
    state = scheduler.fresh_state(["alpha", "beta"])
    order = scheduler.paired_work_order(tmp_path, state, ["alpha", "beta"], {})
    assert order[:3] == [
        (f"alpha/{arm}", "alpha", 1) for arm in arms if arm != "baseline"
    ]
    assert ("alpha/baseline", "alpha", 1) not in order
    assert ("alpha/baseline", "alpha-EVO-1", 2) in order
    assert order[3:7] == [(f"beta/{arm}", "beta", 1) for arm in arms]


def test_paused_arm_is_not_scheduled_and_can_be_restored(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(scheduler, "chain_tasks", lambda chain: [chain])
    state_path = tmp_path / "state.json"
    original = scheduler.fresh_state(["demo"])
    original["work"]["demo/open-ended"]["attempts"] = 2
    state_path.write_text(json.dumps(original))

    monkeypatch.setattr(
        scheduler,
        "ARMS",
        {arm: variant for arm, variant in scheduler.ALL_ARMS.items() if arm != "open-ended"},
    )
    paused = scheduler.load_state(state_path, ["demo"], False)
    assert len(paused["work"]) == 3
    assert paused["paused_work"]["demo/open-ended"]["attempts"] == 2
    assert all("open-ended" not in key for key in paused["work"])
    assert all("open-ended" not in key for key, *_ in scheduler.paired_work_order(
        tmp_path, paused, ["demo"], {}
    ))

    state_path.write_text(json.dumps(paused))
    monkeypatch.setattr(scheduler, "ARMS", scheduler.ALL_ARMS.copy())
    restored = scheduler.load_state(state_path, ["demo"], False)
    assert len(restored["work"]) == 4
    assert restored["work"]["demo/open-ended"]["attempts"] == 2
    assert not restored["paused_work"]


def test_stage_launch_limits_chain_runner_to_one_task(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    commands = []

    class Worker:
        pid = 123

    monkeypatch.setattr(
        scheduler.subprocess,
        "Popen",
        lambda command, **kwargs: commands.append(command) or Worker(),
    )
    scheduler.launch(tmp_path, 8040, "demo", "baseline", 2)
    assert commands[0][-1] == "2"
    scheduler.launch(tmp_path, 8040, "demo", "baseline")
    assert len(commands[1]) == 5


def test_chain_priority_finishes_active_then_partial_and_skips_blocked(
    tmp_path, monkeypatch
):
    scheduler = module("supervise-terminal-full.py")
    chains = ["new", "blocked", "partial", "active"]
    monkeypatch.setattr(
        scheduler, "chain_tasks", lambda chain: [chain, f"{chain}-EVO-1"]
    )
    completed = {
        ("partial", "baseline"): {"partial", "partial-EVO-1"},
        ("partial", "fixed-loop"): {"partial"},
        ("partial", "self-action"): {"partial"},
        ("partial", "open-ended"): {"partial"},
        ("active", "baseline"): {"active"},
        ("blocked", "baseline"): {"blocked"},
    }
    monkeypatch.setattr(
        scheduler,
        "completed_task_names",
        lambda path: completed.get((path.parent.name, path.name), set()),
    )
    state = scheduler.fresh_state(chains)
    state["work"]["active/baseline"]["status"] = "running"
    state["work"]["blocked/fixed-loop"]["status"] = "incomplete"

    class Worker:
        def poll(self):
            return None

    active = {"8040-1": (8040, "active", "baseline", Worker())}
    order = scheduler.chain_priority_order(tmp_path, state, chains, active)
    assert [item[0].split("/", 1)[0] for item in order[:3]] == ["active"] * 3
    assert [item[0].split("/", 1)[0] for item in order[3:6]] == ["partial"] * 3
    assert all(not item[0].startswith("blocked/") for item in order)
    assert order[3:6] == [
        (f"partial/{arm}", "partial-EVO-1", 2)
        for arm in ("fixed-loop", "self-action", "open-ended")
    ]
    assert order[-4:] == [(f"new/{arm}", "new", 1) for arm in scheduler.ARMS]


def test_chain_priority_soft_cap_fills_otherwise_idle_endpoint():
    scheduler = module("supervise-terminal-full.py")
    candidates = [("old/baseline", "old", 1), ("next/baseline", "next", 1)]
    assert (
        scheduler.select_chain_candidate(
            candidates, {"old", "another"}, 2, lambda item: item[0] == "next/baseline"
        )
        == candidates[1]
    )
    assert (
        scheduler.select_chain_candidate(
            candidates, {"old", "another"}, 2, lambda item: True
        )
        == candidates[0]
    )


def test_chain_image_gate_checks_all_unfinished_stages(tmp_path, monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(scheduler, "chain_tasks", lambda chain: ["base", "evo"])
    state = scheduler.fresh_state(["demo"])
    state["work"]["demo/baseline"]["status"] = "complete"
    monkeypatch.setattr(
        scheduler,
        "work_progress",
        lambda root, chain, arm: {"missing": [] if arm == "baseline" else ["evo"]},
    )
    checked = []

    def evidence(root, task):
        checked.append(task)
        return {"status": "ready", "image": "img", "image_id": "id"}

    monkeypatch.setattr(scheduler, "readiness", evidence)

    class Image:
        returncode = 0
        stdout = "id\n"

    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **kw: Image())
    assert scheduler.chain_images_ready(tmp_path, "demo", state)
    assert checked == ["evo"]


@pytest.mark.parametrize(
    "payload", [{}, {"pid": 123, "time": 1}, {"pid": 999, "time": time.time()}]
)
def test_heartbeat_checks_pid_and_age(tmp_path, payload):
    guard = module("watch-terminal-infrastructure.py")
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(payload))
    assert guard.heartbeat_age(path, 123, time.time() - 400) > 300


def test_preflight_rejects_empty_or_broken_tests(tmp_path):
    preflight = module("preflight-terminal-environments.py")
    (tmp_path / "verifier").mkdir()
    (tmp_path / "agent").mkdir()
    (tmp_path / "result.json").write_text('{"exception_info": null}')
    report = tmp_path / "verifier/ctrf.json"
    report.write_text(json.dumps({"results": {"summary": {"tests": 0}}}))
    with pytest.raises(RuntimeError):
        preflight.test_report(tmp_path)
    report.write_text(json.dumps({"results": {"summary": {"tests": 4, "failed": 4}}}))
    (tmp_path / "agent/infrastructure.json").write_text(
        '{"tmux": "passed", "image": "img"}'
    )
    assert preflight.test_report(tmp_path)["tests"]["failed"] == 4
    (tmp_path / "result.json").write_text(
        '{"exception_info": {"exception_type": "VerifierTimeout"}}'
    )
    with pytest.raises(RuntimeError):
        preflight.test_report(tmp_path)


def test_build_template_uses_stable_image_tag():
    import yaml

    template = (
        infra.TERMINAL
        / "harbor/src/harbor/environments/docker/docker-compose-build.yaml"
    )
    service = yaml.safe_load(template.read_text())["services"]["main"]
    assert service["image"] == "${MAIN_IMAGE_NAME}"


@pytest.mark.parametrize(
    "kind,expected",
    [("AgentTimeoutError", None), ("EnvironmentStartTimeoutError", "new")],
)
def test_recovery_requires_repaired_environment_not_model_retry(
    tmp_path, monkeypatch, kind, expected
):
    scheduler = module("supervise-terminal-full.py")
    monkeypatch.setattr(scheduler, "work_progress", lambda *a: {"missing": ["demo"]})
    monkeypatch.setattr(
        scheduler,
        "readiness",
        lambda *a: {
            "status": "ready",
            "fingerprint": "new",
            "finished": time.time() + 100,
        },
    )
    trial = tmp_path / "demo/baseline/demo__abc"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"task_name": "demo", "exception_info": {"exception_type": kind}})
    )
    assert (
        scheduler.repaired_environment_ticket(tmp_path, "demo", "baseline") == expected
    )


def test_adopted_worker_rejects_pid_reuse(monkeypatch):
    scheduler = module("supervise-terminal-full.py")
    identities = iter(["old", "old", "reused"])
    monkeypatch.setattr(
        scheduler.AdoptedWorker, "_identity", lambda self: next(identities)
    )
    process = scheduler.AdoptedWorker(123)
    assert process.poll() is None
    assert process.poll() == 0


def test_cross_arm_parallelism_never_duplicates_same_work():
    scheduler = module("supervise-terminal-full.py")
    active = {"8040-1": (8040, "demo", "baseline", None)}
    assert scheduler.worker_conflicts(active, "demo", "baseline", True)
    assert not scheduler.worker_conflicts(active, "demo", "fixed-loop", True)
    assert scheduler.worker_conflicts(active, "demo", "fixed-loop", False)
