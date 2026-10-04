from types import SimpleNamespace

from rsi_harness import clean_campaign as campaign
from rsi_harness.clean_rsi import ARMS, atomic_json


def test_existing_record_never_resolves_or_rechecks_environment(tmp_path, monkeypatch):
    args = SimpleNamespace(root=tmp_path, chain="chain", arm="skill-rsi", task="one")
    record = campaign.stage_path(tmp_path, "chain", "skill-rsi", "one") / "record.json"
    atomic_json(record, {"status": "complete", "reward": 0})

    def forbidden(*args, **kwargs):
        raise AssertionError("completed task must never execute again")

    monkeypatch.setattr(campaign, "ready", forbidden)
    monkeypatch.setattr(campaign, "tasks", forbidden)
    assert campaign.run_stage(args) == 0


def test_chain_predecessor_must_finish_before_next_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "tasks", lambda _: ["one", "two"])
    args = SimpleNamespace(root=tmp_path, chain="chain", arm="skill-rsi", task="two")
    import pytest

    with pytest.raises(RuntimeError, match="predecessor"):
        campaign.run_stage(args)
    assert not list(tmp_path.rglob("launch.json"))


def test_invalid_old_baseline_is_excluded_not_resolved(tmp_path, monkeypatch):
    path = tmp_path / "chain/baseline/old/result.json"
    atomic_json(
        path,
        {
            "task_name": "one",
            "exception_info": None,
            "verifier_result": {"rewards": {"reward": 1}},
        },
    )
    monkeypatch.setattr(campaign, "baseline_compatibility", lambda *_: ["max_turns"])
    record = campaign.legacy_baseline(tmp_path, "chain", "one")
    assert record["status"] == "excluded"
    assert record["audit"][0]["reasons"] == ["max_turns"]


def test_status_pairs_only_identical_completed_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "tasks", lambda _: ["one", "two"])
    for arm in ARMS:
        atomic_json(
            campaign.stage_path(tmp_path, "chain", arm, "one") / "record.json",
            {"status": "complete", "reward": int(arm != "baseline")},
        )
    atomic_json(
        campaign.stage_path(tmp_path, "chain", "baseline", "two") / "record.json",
        {"status": "excluded"},
    )
    _, pending, blocked, summary = campaign.status(tmp_path, ["chain"])
    assert summary["paired_tasks"] == 1
    assert summary["paired_passed"] == {"baseline": 0, "skill-rsi": 1, "harness-rsi": 1}
    assert summary["baseline"]["complete_chains"] == 0
    assert summary["baseline"]["excluded"] == 1
    assert len(pending) == 2 and not blocked


def test_blocked_stage_does_not_schedule_successor(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "tasks", lambda _: ["one", "two"])
    atomic_json(
        campaign.stage_path(tmp_path, "chain", "harness-rsi", "one") / "record.json",
        {"status": "blocked", "reason": "trial_exception"},
    )
    _, pending, blocked, _ = campaign.status(tmp_path, ["chain"])
    assert not any(arm == "harness-rsi" for _, arm, _ in pending)
    assert len(blocked) == 1


def test_reward_alone_is_not_valid_grader_evidence(tmp_path):
    result = tmp_path / "result.json"
    atomic_json(result, {"verifier_result": {"rewards": {"reward": 0}}})
    assert campaign.scored_result(result) == (False, "verifier_incomplete")
    atomic_json(
        tmp_path / "verifier/ctrf.json",
        {"results": {"summary": {"tests": 1, "failed": 1, "passed": 0}}},
    )
    assert campaign.scored_result(result) == (True, None)
    (tmp_path / "verifier/test-stdout.txt").write_text("No space left on device")
    assert not campaign.scored_result(result)[0]


def test_agent_budget_exhaustion_is_not_an_environment_failure(tmp_path):
    result = tmp_path / "result.json"
    atomic_json(
        tmp_path / "verifier/ctrf.json",
        {"results": {"summary": {"tests": 1, "failed": 1, "passed": 0}}},
    )
    for kind in ("AgentTimeoutError", "OutputLengthExceededError"):
        atomic_json(
            result,
            {
                "verifier_result": {"rewards": {"reward": 0}},
                "exception_info": {"exception_type": kind},
            },
        )
        assert campaign.scored_result(result) == (True, None)
    atomic_json(
        result,
        {
            "verifier_result": {"rewards": {"reward": 0}},
            "exception_info": {"exception_type": "VerifierTimeoutError"},
        },
    )
    assert campaign.scored_result(result) == (False, "trial_exception")
