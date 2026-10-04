import copy
import json
import time
from pathlib import Path

import pytest

from rsi_harness import meta_skill as m
from rsi_harness.clean_rsi import initialize, load_payload, read_json, select_skills
from rsi_harness.terminal_infra import atomic_json


def documents():
    diagnosis = {"summary": "Public JSON check passed; no general transfer evidence.",
                 "observations": [{"id": "e1", "span_id": "t0000",
                                   "interpretation": "Observed successful public assertion only",
                                   "status": "observed", "resolution": "not_applicable"}]}
    proposal = {"reason": "Separate writing from checking", "upserts": [{
        "id": "json-validation", "title": "Validate JSON output", "level": "meta",
        "when": "When producing JSON under an explicit schema",
        "mechanism": "Parsing can catch invalid JSON; schema validity needs separate checks",
        "steps": ["Parse the actual output", "Compare parsed keys and values to the current spec"],
        "cautions": ["Do not apply JSON parsing to outputs required to be plain text"],
        "evidence_ids": ["e1"], "transfer_check": "Assert the current task's required shape"}]}
    review = {"decisions": [{"id": "json-validation", "accept": True,
                             "reason": "Narrowly scoped to structured JSON", "unsupported_claims": []}]}
    return diagnosis, proposal, review


@pytest.fixture
def trial(tmp_path):
    trial = tmp_path / "trial"
    atomic_json(trial / "agent/trajectory.json", {"steps": [
        {"source": "agent", "message": "public JSON check passed", "reasoning_content": "PRIVATE_CANARY"},
        {"source": "user", "message": "OTHER_CANARY"}]})
    atomic_json(trial / "verifier/ctrf.json", {"hidden": "HIDDEN_CANARY"})
    return trial


class Transport:
    def __init__(self, replies=None, fail=None):
        self.replies = replies or documents()
        self.requests = []
        self.fail = fail

    def __call__(self, base, request, timeout):
        self.requests.append(copy.deepcopy(request))
        assert 0 < timeout <= 600
        if self.fail:
            raise self.fail
        return {"choices": [{"message": {"content": json.dumps(self.replies[len(self.requests)-1])},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}


def update(tmp_path, trial, transport, task="a"):
    return m.improve(tmp_path / "state", task, "Create JSON output", trial, 1,
                     "http://unused/v1", "frozen-model", transport=transport)


def test_accepts_three_calls_and_consumes_same_retriever(tmp_path, trial):
    t = Transport()
    r = update(tmp_path, trial, t)
    assert r["accepted"] and r["after"]["revision"] == 1
    assert r["accounting"]["calls"] == 3
    assert [v["max_tokens"] for v in t.requests] == [2048, 4096, 2048]
    assert sum(v["max_tokens"] for v in t.requests) == 8192
    assert all(v["temperature"] == .2 and not v["chat_template_kwargs"]["enable_thinking"] for v in t.requests)
    payload = load_payload(tmp_path / "state", r["after"])
    assert select_skills(payload, "JSON output")[0] == ["json-validation"]
    assert "NOT been validated" in payload["json-validation"]
    assert not any(x in str(t.requests) for x in ("HIDDEN_CANARY", "PRIVATE_CANARY", "OTHER_CANARY"))


def test_completed_update_never_resampled(tmp_path, trial):
    t = Transport()
    one = update(tmp_path, trial, t)
    assert update(tmp_path, trial, t) == one
    assert len(t.requests) == 3


def test_timeout_rejected_no_resampling(tmp_path, trial):
    t = Transport(fail=TimeoutError("timed out"))
    r = update(tmp_path, trial, t)
    assert not r["accepted"] and r["before"] == r["after"]
    assert r["accounting"]["calls"] == 1 and not r["accounting"]["usage_complete"]
    update(tmp_path, trial, t)
    assert len(t.requests) == 1


def test_interrupted_request_never_repeated(tmp_path, trial):
    class Crash(BaseException):
        pass
    t = Transport(fail=Crash())
    with pytest.raises(Crash):
        update(tmp_path, trial, t)
    recovered = update(tmp_path, trial, Transport())
    assert "interrupted_request_not_repeated" in recovered["reason"]
    assert recovered["accounting"]["calls"] == 1


def test_wall_clock_budget_across_phases(tmp_path, trial, monkeypatch):
    real = time.time()
    clock = [real]
    monkeypatch.setattr(m.time, "time", lambda: clock[0])
    t = Transport()
    def slow(*args):
        response = t(*args)
        clock[0] += 601
        return response
    r = update(tmp_path, trial, slow)
    assert not r["accepted"] and r["accounting"]["calls"] == 1


def test_literal_evidence_gate(tmp_path, trial):
    replies = list(documents())
    replies[0]["observations"][0]["span_id"] = "t9999"
    r = update(tmp_path, trial, Transport(replies))
    assert not r["accepted"] and "ungrounded evidence" in r["reason"]
    assert r["accounting"]["calls"] == 1


def test_unknown_evidence_id(tmp_path, trial):
    replies = list(documents())
    replies[1]["upserts"][0]["evidence_ids"] = ["invented"]
    r = update(tmp_path, trial, Transport(replies))
    assert not r["accepted"] and r["accounting"]["calls"] == 2


def test_reviewer_rejects_overgeneralization(tmp_path, trial):
    replies = list(documents())
    replies[2]["decisions"][0].update(accept=False, unsupported_claims=["universal claim"])
    r = update(tmp_path, trial, Transport(replies))
    assert not r["accepted"] and r["before"] == r["after"]


def test_contradictory_review_fails_closed(tmp_path, trial):
    replies = list(documents())
    replies[2]["decisions"][0]["unsupported_claims"] = ["not supported"]
    r = update(tmp_path, trial, Transport(replies))
    assert not r["accepted"] and "accepted unsupported" in r["reason"]


def test_no_change_uses_two_calls(tmp_path, trial):
    d, p, r = documents()
    p["upserts"] = []
    result = update(tmp_path, trial, Transport((d, p, r)))
    assert not result["accepted"] and result["reason"] == "no_change_proposed"
    assert result["accounting"]["calls"] == 2


def test_truncated_response_not_deployed(tmp_path, trial):
    t = Transport()
    def truncate(*args):
        response = t(*args)
        response["choices"][0]["finish_reason"] = "length"
        return response
    r = update(tmp_path, trial, truncate)
    assert not r["accepted"] and "incomplete_response" in r["reason"]


def test_commit_recovery_does_not_resample(tmp_path, trial, monkeypatch):
    original = m.commit
    class Crash(BaseException):
        pass
    def crash(*args):
        original(*args)
        raise Crash()
    monkeypatch.setattr(m, "commit", crash)
    t = Transport()
    with pytest.raises(Crash):
        update(tmp_path, trial, t)
    monkeypatch.setattr(m, "commit", original)
    r = update(tmp_path, trial, t)
    assert r["accepted"] and r["reason"] == "recovered_commit"
    assert len(t.requests) == 3


def test_chain_reset_empty_and_independent(tmp_path, trial):
    update(tmp_path, trial, Transport())
    other = tmp_path / "other-chain"
    head = initialize(other, "skill-rsi")
    assert head["revision"] == 0 and load_payload(other, head) == {}


@pytest.mark.parametrize("mutation", ["missing_boundary", "unknown_level", "path_id", "too_many", "too_large"])
def test_candidate_constraints(mutation):
    d, p, _ = documents()
    if mutation == "missing_boundary": p["upserts"][0]["cautions"] = []
    if mutation == "unknown_level": p["upserts"][0]["level"] = "harness"
    if mutation == "path_id": p["upserts"][0]["id"] = "../escape"
    if mutation == "too_many": p["upserts"] *= 4
    if mutation == "too_large": p["upserts"][0]["mechanism"] = "x" * 32769
    with pytest.raises(ValueError):
        m.proposal_check(p, d, {})


def test_same_public_window_cap(tmp_path):
    trial = tmp_path / "trace"
    atomic_json(trial / "agent/trajectory.json", {"steps": [
        {"source": "agent", "message": "x" * 50000}]})
    assert len(m.public_trace(trial)) == 24000


def test_review_cannot_invent_or_omit_candidates():
    d, p, r = documents()
    docs = m.proposal_check(p, d, {})
    with pytest.raises(ValueError):
        m.review_check({"decisions": []}, docs)
    r["decisions"][0]["id"] = "unproposed"
    with pytest.raises(ValueError):
        m.review_check(r, docs)


def test_expired_deadline():
    with pytest.raises(TimeoutError):
        with m.deadline(0):
            pytest.fail("must not enter")


def test_prior_library_preserved(tmp_path, trial):
    update(tmp_path, trial, Transport())
    replies = list(documents())
    replies[1]["upserts"][0]["id"] = "second-skill"
    replies[2]["decisions"][0]["id"] = "second-skill"
    result = update(tmp_path, trial, Transport(replies), "b")
    assert set(load_payload(tmp_path / "state", result["after"])) == {"json-validation", "second-skill"}


def test_real_wall_deadline_interrupts():
    with pytest.raises(TimeoutError):
        with m.deadline(.01):
            time.sleep(.05)


def test_cached_completed_phase_can_resume(tmp_path, trial, monkeypatch):
    original = m.atomic_json
    class Crash(BaseException):
        pass
    def crash(path, value):
        if path.name == "finished.json" and path.parent.name == "diagnose":
            raise Crash()
        return original(path, value)
    t = Transport()
    monkeypatch.setattr(m, "atomic_json", crash)
    with pytest.raises(Crash):
        update(tmp_path, trial, t)
    monkeypatch.setattr(m, "atomic_json", original)
    result = update(tmp_path, trial, t)
    assert result["accepted"] and len(t.requests) == 3


def test_committed_head_never_reported_as_rejected(tmp_path, trial, monkeypatch):
    original = m.commit
    def after_commit(*args):
        original(*args)
        raise OSError("simulated post-commit error")
    monkeypatch.setattr(m, "commit", after_commit)
    result = update(tmp_path, trial, Transport())
    assert result["accepted"] and result["after"] == read_json(tmp_path / "state/HEAD.json")


def test_structured_response_required(tmp_path, trial):
    t = Transport()
    update(tmp_path, trial, t)
    for phase, request in zip(("diagnose", "propose", "review"), t.requests):
        spec = request["response_format"]["json_schema"]
        assert spec["strict"] and spec["schema"] == m.OUTPUT_SCHEMAS[phase]


def test_immutable_commit_cannot_overwrite(tmp_path):
    d, p, _ = documents()
    doc = m.proposal_check(p, d, {})
    head = m.commit(tmp_path, "skill-rsi", doc, "a", 1)
    with pytest.raises(ValueError, match="collision"):
        m.commit(tmp_path, "skill-rsi", {}, "b", 1)
    assert read_json(tmp_path / "HEAD.json") == head


def test_partial_review_keeps_only_accepted_candidate(tmp_path, trial):
    d, p, r = documents()
    two = copy.deepcopy(p["upserts"][0])
    two["id"] = "reject-me"
    p["upserts"].append(two)
    r["decisions"].append({"id": "reject-me", "accept": False, "reason": "Redundant",
                           "unsupported_claims": []})
    result = update(tmp_path, trial, Transport((d, p, r)))
    assert result["accepted"] and result["accepted_ids"] == ["json-validation"]
    assert set(load_payload(tmp_path / "state", result["after"])) == {"json-validation"}


def test_numbered_evidence_is_lossless_and_host_anchored():
    trace = 'some quotes "inside" escaped \\n text ' * 1000
    trace = trace[-24000:]
    spans = m.trace_spans(trace)
    assert ''.join(s["quote"] for s in spans.values()) == trace
    d, _, _ = documents()
    d["observations"][0]["span_id"] = "t0003"
    checked = m.diagnosis_check(d, trace)
    event = checked["observations"][0]
    assert event["quote"] == trace[event["start"]:event["end"]]
