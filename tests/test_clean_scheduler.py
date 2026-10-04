import fcntl
from types import SimpleNamespace

import pytest

from rsi_harness import clean_scheduler as scheduler
from rsi_harness.clean_rsi import ARMS, atomic_json, read_json


@pytest.fixture
def series(monkeypatch):
    monkeypatch.setattr(
        scheduler.core, "tasks", lambda chain: [f"{chain}-1", f"{chain}-2"]
    )
    return ["A", "B", "C", "D"]


def row(chain, arm, stage, status="complete", reward=1):
    return {
        "chain": chain,
        "arm": arm,
        "task": f"{chain}-{stage}",
        "status": status,
        "reward": reward,
    }


def complete_chain(chain):
    return [row(chain, arm, stage) for arm in ARMS for stage in (1, 2)]


def worker(chain, arm, stage=1, pid=100, identity="start"):
    return {
        "chain": chain,
        "arm": arm,
        "task": f"{chain}-{stage}",
        "pid": pid,
        "identity": identity,
    }


def plan(order, rows=(), pending=(), active=None, check=None):
    return scheduler.window_plan(
        order, rows, pending, active or {}, check or (lambda _: (True, None))
    )


def test_common_order_and_lagging_arm_take_priority(series):
    pending = [
        ("B", "skill-rsi", "B-1"),
        ("A", "baseline", "A-2"),
        ("A", "harness-rsi", "A-1"),
    ]
    out = plan(series, pending=pending)
    assert out["window"] == ["A", "B", "C"]
    assert out["candidates"] == [pending[2], pending[1], pending[0]]


def test_busy_lagging_arm_does_not_force_fast_arm_to_wait(series):
    pending = [("A", "skill-rsi", "A-1"), ("A", "harness-rsi", "A-2")]
    active = {"8040": worker("A", "skill-rsi")}
    out = plan(series, pending=pending, active=active)
    assert out["candidates"] == [pending[1]]


def test_spare_slot_can_take_third_chain_but_not_fourth(series):
    pending = [(c, arm, f"{c}-1") for c in series for arm in ARMS]
    active = {
        str(i): worker(c, arm)
        for i, (c, arm) in enumerate((c, a) for c in ("A", "B") for a in ARMS)
    }
    out = plan(series, pending=pending, active=active)
    assert {c for c, _, _ in out["candidates"]} == {"C"}
    assert all(c == "D" for c, _, _ in out["window_limited"])


def test_completed_ahead_chains_still_consume_lookahead_positions(series):
    rows = complete_chain("B") + complete_chain("C")
    pending = [("A", "skill-rsi", "A-1"), ("D", "skill-rsi", "D-1")]
    out = plan(series, rows, pending)
    assert out["window"] == ["A", "B", "C"]
    assert out["candidates"] == [pending[0]]
    advanced = plan(series, rows + complete_chain("A"), pending[1:])
    assert advanced["window"] == ["D"]


def test_baseline_completion_alone_does_not_advance_window(series):
    rows = [row(c, "baseline", i) for c in series for i in (1, 2)]
    assert plan(series, rows)["window"] == ["A", "B", "C"]


def test_bad_environment_is_deferred_for_all_arms_not_counted_complete(series):
    out = plan(
        series, check=lambda c: (c != "A", "preflight_failed" if c == "A" else None)
    )
    assert out["window"] == ["B", "C", "D"]
    assert out["deferred"] == [{"chain": "A", "reason": "preflight_failed"}]


def test_blocked_chain_is_quarantined_without_killing_active_work(series):
    active = {"8040": worker("A", "harness-rsi")}
    out = plan(
        series,
        [row("A", "skill-rsi", 1, "blocked")],
        [("A", "baseline", "A-1"), ("B", "baseline", "B-1")],
        active,
    )
    assert out["quarantined"] == ["A"]
    assert out["candidates"] == [("B", "baseline", "B-1")]
    assert out["preserved_active_outside_window"] == ["A"]
    assert active["8040"]["pid"] == 100


def baseline_fixture(tmp_path, monkeypatch):
    root, old = tmp_path / "run", tmp_path / "old"
    (old / "A/baseline").mkdir(parents=True)
    monkeypatch.setattr(scheduler.core, "ready", lambda *_: (True, None))
    monkeypatch.setattr(scheduler.core, "readiness", lambda *_: {"status": "ready"})
    return root, old


def test_baseline_adoption_does_not_start_process_or_use_service(
    tmp_path, monkeypatch, series
):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        scheduler.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("no worker allowed"),
    )
    active = {"8040": worker("A", "skill-rsi")}
    lookup = lambda *_: {"status": "complete", "source": "legacy_reuse", "reward": 0}
    adopted, waiting = scheduler.adopt_baselines(root, old, ["A"], active, lookup)
    assert len(adopted) == 2 and not waiting and len(active) == 1
    assert read_json(root / "A/baseline/stages/A-1/record.json")["reward"] == 0
    assert scheduler.adopt_baselines(root, old, ["A"], active, lookup)[0] == []


def test_adoption_preserves_live_or_launched_baseline(tmp_path, monkeypatch, series):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    calls = []
    lookup = lambda *a: calls.append(a)
    scheduler.adopt_baselines(
        root, old, ["A"], {"8040": worker("A", "baseline")}, lookup
    )
    assert not calls
    atomic_json(root / "A/baseline/stages/A-1/launch.json", {"started": 1})
    scheduler.adopt_baselines(root, old, ["A"], {}, lookup)
    assert not calls and not list(root.rglob("record.json"))


def test_adoption_respects_lock_and_missing_predecessor(tmp_path, monkeypatch, series):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    work = root / "A/baseline"
    work.mkdir(parents=True)
    with (work / "worker.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (
            scheduler.adopt_baselines(
                root, old, ["A"], {}, lambda *_: pytest.fail("locked")
            )[0]
            == []
        )
    seen = []
    scheduler.adopt_baselines(
        root, old, ["A"], {}, lambda _old, _chain, task: seen.append(task)
    )
    assert seen == ["A-1"]


def test_adoption_failure_does_not_create_false_zero(tmp_path, monkeypatch, series):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler.core, "ready", lambda *_: (False, "image_changed"))
    adopted, waiting = scheduler.adopt_baselines(
        root, old, ["A"], {}, lambda *_: {"status": "complete"}
    )
    assert not adopted and waiting[0]["reason"] == "image_changed"
    assert not list(root.rglob("record.json"))


def test_paired_metrics_never_compare_different_task_sets(series):
    rows = [row("A", a, 1, reward=int(a != "baseline")) for a in ARMS]
    rows += [row("A", "baseline", 2), row("A", "skill-rsi", 2)]
    out = scheduler.paired_metrics(rows, ["A"])
    assert out["n_tasks"] == 1
    assert out["passed"] == {"baseline": 0, "skill-rsi": 1, "harness-rsi": 1}
    assert out["complete_chains"] == []
    assert scheduler.paired_metrics(rows + [row("A", "harness-rsi", 2)], ["A"])[
        "complete_chains"
    ] == ["A"]


def test_manifest_preserves_protocol_and_freezes_scheduler(
    tmp_path, monkeypatch, series
):
    monkeypatch.setattr(scheduler.core, "chains", lambda: series)
    args = SimpleNamespace(root=tmp_path, old_root=tmp_path / "old", pilot=["A"])
    original = scheduler.protocol_manifest(args)
    atomic_json(tmp_path / "manifest.json", original)
    original_bytes = (tmp_path / "manifest.json").read_bytes()
    assert scheduler.initialize_scheduler(args) == series
    assert scheduler.initialize_scheduler(args) == series
    assert (tmp_path / "manifest.json").read_bytes() == original_bytes
    saved = read_json(tmp_path / "scheduler_manifest.json")
    saved["policy"]["window_chains"] = 10
    atomic_json(tmp_path / "scheduler_manifest.json", saved)
    with pytest.raises(RuntimeError, match="scheduler policy"):
        scheduler.initialize_scheduler(args)


def test_manifest_rejects_algorithm_changes(tmp_path, monkeypatch, series):
    monkeypatch.setattr(scheduler.core, "chains", lambda: series)
    args = SimpleNamespace(root=tmp_path, old_root=tmp_path / "old", pilot=["A"])
    original = scheduler.protocol_manifest(args)
    atomic_json(tmp_path / "manifest.json", original)
    monkeypatch.setattr(scheduler.core, "source_hashes", lambda: {"changed": "hash"})
    with pytest.raises(RuntimeError, match="frozen experiment protocol"):
        scheduler.initialize_scheduler(args)


def test_restart_preserves_live_worker_and_child(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scheduler.core,
        "process_identity",
        lambda pid: {100: "start", 201: "child"}.get(pid),
    )
    active = {
        "8040": worker("A", "skill-rsi"),
        "8041": worker("B", "harness-rsi", pid=200),
    }
    atomic_json(
        tmp_path / "B/harness-rsi/stages/B-1/child.json",
        {"pid": 201, "identity": "child"},
    )
    scheduler.reap_workers(tmp_path, active)
    assert len(active) == 2 and not list(tmp_path.rglob("record.json"))


def test_dead_worker_is_not_blindly_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.core, "process_identity", lambda _: None)
    active = {"8040": worker("A", "skill-rsi", identity=None)}
    scheduler.reap_workers(tmp_path, active)
    assert not active
    assert (
        read_json(tmp_path / "A/skill-rsi/stages/A-1/record.json")["status"]
        == "blocked"
    )


def test_real_scheduler_cycle_adopts_baselines_and_dispatches_only_four_stages(
    tmp_path, monkeypatch, series
):
    monkeypatch.setattr(scheduler.core, "chains", lambda: series)
    root, old = baseline_fixture(tmp_path, monkeypatch)
    root.mkdir(parents=True)
    for chain in series:
        (old / chain / "baseline").mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(root=root, old_root=old, pilot=["A", "B"])
    original = scheduler.protocol_manifest(args)
    atomic_json(root / "manifest.json", original)
    monkeypatch.setattr(scheduler.core, "pilot_gate", lambda *_: {"passed": True})
    monkeypatch.setattr(scheduler.core, "live_chain_workers", lambda *_: [])
    monkeypatch.setattr(scheduler.core, "docker_disk_status", lambda *_: {"ok": True})
    monkeypatch.setattr(scheduler.core, "endpoint_ok", lambda *_: True)
    monkeypatch.setattr(scheduler.signal, "signal", lambda *_: None)
    original_adopt = scheduler.adopt_baselines
    monkeypatch.setattr(
        scheduler,
        "adopt_baselines",
        lambda r, o, chains, active: original_adopt(
            r,
            o,
            chains,
            active,
            lambda *_: {
                "status": "complete",
                "source": "legacy_reuse",
                "reward": 1,
            },
        ),
    )
    commands = []

    def spawn(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=100 + len(commands))

    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    monkeypatch.setattr(scheduler.core, "process_identity", lambda _: "start")

    class EndCycle(Exception):
        pass

    def stop_cycle(_seconds):
        raise EndCycle

    monkeypatch.setattr(scheduler.time, "sleep", stop_cycle)
    with pytest.raises(EndCycle):
        scheduler.supervise(args)
    assert len(commands) == 4
    assert all(
        command[3:5] == ["rsi_harness.clean_scheduler", "--stage"]
        for command in commands
    )
    assert all(
        command[command.index("--arm") + 1] != "baseline" for command in commands
    )
    snapshot = read_json(root / "status.json")
    assert snapshot["summary"]["baseline"]["completed"] == 8
    assert snapshot["scheduling"]["window"] == ["A", "B", "C"]
    assert {(w["chain"], w["arm"]) for w in snapshot["active"].values()} == {
        (c, a) for c in ("A", "B") for a in ("skill-rsi", "harness-rsi")
    }
    assert scheduler.digest(read_json(root / "manifest.json")) == scheduler.digest(
        original
    )


def test_excluded_baseline_is_pending_and_keeps_completed_successors(tmp_path, series):
    for arm in ARMS:
        for stage in (1, 2):
            state = "excluded" if arm == "baseline" and stage == 1 else "complete"
            atomic_json(
                tmp_path / f"A/{arm}/stages/A-{stage}/record.json",
                {"status": state, "reward": 1},
            )
    _rows, pending, blocked, summary = scheduler.campaign_status(tmp_path, ["A"])
    assert pending == [("A", "baseline", "A-1")]
    assert not blocked
    assert summary["baseline"]["completed"] == 1
    assert summary["baseline"]["pending_repairs"] == 1
    assert summary["paired_tasks"] == 1
    folder = tmp_path / "A/baseline/stages/A-1"
    (folder / "record.json").replace(folder / "excluded_record.json")
    _rows, pending, blocked, summary = scheduler.campaign_status(tmp_path, ["A"])
    assert pending == [("A", "baseline", "A-1")]
    assert summary["baseline"]["completed"] == 1
    assert summary["baseline"]["pending_repairs"] == 1
    assert summary["paired_tasks"] == 1


def test_repair_queue_has_priority_and_preserves_regular_window(series):
    rows = [row("A", a, s) for a in ARMS[1:] for s in (1, 2)]
    rows += [row("A", "baseline", 1, "excluded"), row("A", "baseline", 2)]
    pending = [(c, a, f"{c}-1") for c in series[1:] for a in ARMS]
    pending.append(("A", "baseline", "A-1"))
    out = plan(series, rows, pending)
    assert out["baseline_repair_chains"] == ["A"]
    assert out["window"] == ["B", "C", "D"]
    assert out["candidates"][0] == pending[-1]
    assert len(out["candidates"]) == 10
    active = {"8040": worker("A", "baseline")}
    busy = plan(series, rows, pending, active)
    assert pending[-1] not in busy["candidates"]
    assert active["8040"]["pid"] == 100


def test_incompatible_adoption_is_archived_without_settling(
    tmp_path, monkeypatch, series
):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    invalid = {
        "status": "excluded",
        "source": "legacy_incompatible",
        "audit": ["budget"],
    }
    adopted, waiting = scheduler.adopt_baselines(
        root, old, ["A"], {}, lambda *_: invalid
    )
    assert not adopted and not waiting
    folder = root / "A/baseline/stages/A-1"
    assert not (folder / "record.json").exists()
    assert read_json(folder / "excluded_record.json")["audit"] == ["budget"]
    scheduler.adopt_baselines(
        root, old, ["A"], {}, lambda *_: pytest.fail("already audited")
    )
    assert ("A", "baseline", "A-1") in scheduler.campaign_status(root, ["A"])[1]


def test_repair_worker_archives_exclusion_and_does_not_repeat_complete(
    tmp_path, monkeypatch, series
):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    folder = root / "A/baseline/stages/A-1"
    invalid = {"status": "excluded", "source": "legacy_incompatible"}
    atomic_json(folder / "record.json", invalid)
    atomic_json(
        root / "A/baseline/stages/A-2/record.json", {"status": "complete", "reward": 0}
    )
    args = SimpleNamespace(
        root=root, old_root=old, chain="A", arm="baseline", task="A-1"
    )
    monkeypatch.setattr(scheduler, "initialize_scheduler", lambda _: series)
    monkeypatch.setattr(scheduler.core, "legacy_baseline", lambda *_: invalid)
    seen = []

    def solve(args):
        seen.append(args.task)
        assert not (folder / "record.json").exists()
        assert read_json(folder / "excluded_record.json") == invalid
        assert scheduler.core.legacy_baseline(old, "A", "A-1") is None
        atomic_json(folder / "record.json", {"status": "complete", "reward": 1})
        return 0

    monkeypatch.setattr(scheduler.core, "run_stage", solve)
    assert scheduler.run_stage(args) == 0
    assert scheduler.run_stage(args) == 0
    args.task = "A-2"
    assert scheduler.run_stage(args) == 0
    assert seen == ["A-1"]


def test_repair_refuses_existing_launch_and_unrepaired_predecessor(
    tmp_path, monkeypatch, series
):
    root, old = baseline_fixture(tmp_path, monkeypatch)
    folder = root / "A/baseline/stages/A-1"
    atomic_json(folder / "record.json", {"status": "excluded"})
    atomic_json(folder / "launch.json", {"started": 1})
    args = SimpleNamespace(
        root=root, old_root=old, chain="A", arm="baseline", task="A-1"
    )
    monkeypatch.setattr(scheduler, "initialize_scheduler", lambda _: series)
    monkeypatch.setattr(
        scheduler.core, "run_stage", lambda _: pytest.fail("must not solve")
    )
    with pytest.raises(RuntimeError, match="launched trial"):
        scheduler.run_stage(args)
    assert read_json(folder / "record.json")["status"] == "excluded"
    args.task = "A-2"
    with pytest.raises(RuntimeError, match="predecessor"):
        scheduler.run_stage(args)


def test_explicit_migration_keeps_protocol_and_live_worker_ids(
    tmp_path, monkeypatch, series
):
    monkeypatch.setattr(scheduler.core, "chains", lambda: series)
    args = SimpleNamespace(root=tmp_path, old_root=tmp_path / "old", pilot=["A"])
    atomic_json(tmp_path / "manifest.json", scheduler.protocol_manifest(args))
    scheduler.initialize_scheduler(args)
    previous = read_json(tmp_path / "scheduler_manifest.json")
    previous["policy"]["version"] = "paired-window-v1"
    previous["code_sha256"] = scheduler.PREVIOUS_SCHEDULER_SHA256
    atomic_json(tmp_path / "scheduler_manifest.json", previous)
    active = {"8040": worker("B", "skill-rsi")}
    atomic_json(tmp_path / "active.json", active)
    protocol = (tmp_path / "manifest.json").read_bytes()
    with pytest.raises(RuntimeError, match="audit before migration"):
        scheduler.initialize_scheduler(args)
    args.migrate_baseline_repairs = True
    scheduler.initialize_scheduler(args)
    audit = read_json(tmp_path / "scheduler_migrations/baseline-repair-v2.json")
    assert audit["previous_manifest"] == previous
    assert audit["preserved_workers"] == active
    assert (tmp_path / "manifest.json").read_bytes() == protocol
    assert read_json(tmp_path / "active.json") == active


def test_repair_start_failure_is_blocked_instead_of_redispatched(
    tmp_path, monkeypatch, series
):
    folder = tmp_path / "A/baseline/stages/A-1"
    original = {"status": "excluded", "source": "legacy_incompatible"}
    atomic_json(folder / "record.json", original)
    active = {"8040": worker("A", "baseline")}
    monkeypatch.setattr(scheduler.core, "process_identity", lambda _: None)
    scheduler.reap_workers(tmp_path, active)
    _rows, pending, blocked, _summary = scheduler.campaign_status(tmp_path, ["A"])
    assert not active
    assert ("A", "baseline", "A-1") not in pending
    assert blocked[0]["reason"] == "repair_worker_exited_without_replacing_exclusion"
    assert read_json(folder / "record.json") == original
