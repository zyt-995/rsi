from rsi_harness import meta_skill_campaign as c
from rsi_harness.terminal_infra import atomic_json


def test_chain_priority_and_no_cross_stage_leap(tmp_path):
    manifest = {"chain_order": ["a", "b", "c"],
                "task_order": {"a": ["a", "a-1"], "b": ["b", "b-1"], "c": ["c", "c-1"]}}
    atomic_json(c.stage_path(tmp_path, "b", "b") / "record.json", {"status": "complete"})
    done, nexts, blocked, full = c.scan(tmp_path, manifest)
    assert [(r["chain"], r["task"]) for r in nexts] == [("b", "b-1"), ("a", "a"), ("c", "c")]
    assert len(done) == 1 and not blocked and not full


def test_invalid_not_zero_and_chain_quarantined(tmp_path):
    manifest = {"chain_order": ["a"], "task_order": {"a": ["a", "a-1"]}}
    atomic_json(c.stage_path(tmp_path, "a", "a") / "record.json",
                {"status": "blocked", "reason": "infra"})
    done, nexts, blocked, full = c.scan(tmp_path, manifest)
    assert not done and not nexts and len(blocked) == 1 and not full


def test_zero_is_complete_not_retried(tmp_path):
    manifest = {"chain_order": ["a"], "task_order": {"a": ["a", "a-1"]}}
    for task in ("a", "a-1"):
        atomic_json(c.stage_path(tmp_path, "a", task) / "record.json",
                    {"status": "complete", "task": task, "chain": "a", "reward": 0})
    done, nexts, blocked, full = c.scan(tmp_path, manifest)
    assert len(done) == 2 and not nexts and full == ["a"]


def test_metrics_match_exact_same_tasks(tmp_path):
    manifest = {"chain_order": ["a", "b"], "task_order": {"a": ["a"], "b": ["b", "b-1"]}}
    controls = {f"{arm}/{t}": {"reward": 1 if arm == "baseline" else 0}
                for arm in ("baseline", "skill-rsi") for t in ("a", "b", "b-1")}
    for row in controls.values():
        row["result"] = str(tmp_path / "unused-result.json")
    atomic_json(tmp_path / "unused-result.json", {})
    atomic_json(tmp_path / "control_audit.json", {"controls": controls})
    for task in ("a", "b"):
        atomic_json(c.stage_path(tmp_path, task, task) / "record.json",
                    {"status": "complete", "task": task, "chain": task, "reward": 0})
    report = c.metrics(tmp_path, manifest)
    assert report["matched_tasks"]["tasks"] == 2
    assert report["matched_full_chains"]["tasks"] == 1
    assert report["matched_tasks"]["arms"]["baseline"]["mean_reward"] == 1
    assert report["matched_tasks"]["paired_deltas"]["baseline"]["regressed"] == ["a", "b"]
    assert report["blocked"] == []
