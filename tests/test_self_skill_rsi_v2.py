"""No model/Docker calls: tie admission, zero duplicate controls, sealed handoff."""
import copy
from pathlib import Path

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi import bank, campaign as v1, learning
from self_skill_rsi_v2 import campaign as c, gate
from test_self_skill_rsi import DOC, Transport, replies


class Native:
    def __init__(self, score=1, validation_score=None, fail_parent=False):
        self.score, self.validation_score, self.fail_parent = score, validation_score, fail_parent
        self.calls = []

    def __call__(self, entry, store, head, folder, port, purpose):
        saved = read_json(folder / 'record.json')
        if saved:
            return saved
        self.calls.append((entry['uid'], purpose, head['revision'], str(folder)))
        if self.fail_parent and purpose == 'validation' and folder.name == 'parent':
            raise RuntimeError('cancelled redundant parent')
        score = self.score if purpose != 'validation' or self.validation_score is None else self.validation_score
        path = folder / 'trials/fake/result.json'
        atomic_json(path, {'score': score})
        atomic_json(path.parent / 'agent/trajectory.json', {'steps': [
            {'source': 'agent', 'message': 'Public JSON parser failed, then the artifact was fixed.'}]})
        r = {'status': 'complete', 'purpose': purpose, 'uid': entry['uid'], 'task': entry['task'],
             'before': head, 'reward': score, 'result': str(path), 'solve_seconds': 1,
             'hashes': {str(p): file_hash(p) for p in (path, path.parent / 'agent/trajectory.json')}}
        atomic_json(folder / 'record.json', r)
        return r


def proposer(folder, source, old, provenance, history, model, url):
    t = Transport(replies(source['uid'], revised=bool(old)))
    return learning.propose(folder, source, old, provenance, history, model, url, t)


@pytest.fixture
def protocol(tmp_path):
    entries = []
    for n, chain in enumerate(('a', 'a', 'b')):
        task = tmp_path / 'tasks' / f'task-{n}'
        task.mkdir(parents=True)
        (task / 'instruction.md').write_text('Produce valid JSON using the current schema')
        entries.append({'uid': f's{n:06d}', 'chain': chain, 'task': task.name,
                        'path': str(task), 'version': 1 if n == 1 else 0})
    root = tmp_path / 'run'
    bank.initialize(root / 'bank')
    m = {'schema': c.PROTOCOL, 'model': 'same-frozen-model', 'stream': entries, 'heldout': [],
         'ports': [8040, 8041, 8042, 8044], 'gate': gate.DEFAULT,
         'request': {'chains': ['a', 'b']}, 'claims': 'test',
         'migration': {'official_prefix_scored_under_v1': 0, 'source_root': str(tmp_path / 'unrelated-history')}}
    return root, m


def step(root, m, index, native):
    return c.step(root, m, index, solve=native, proposer=proposer, checker=lambda _: None)


def comparison(tmp_path, uid, a=1, b=1):
    entry = {'uid': uid, 'task': uid}
    head = {'revision': 0}
    ref = Native(a)(entry, None, head, tmp_path / uid / 'reference', 8040, 'stream')
    candidate = Native(b)(entry, None, {'revision': 1}, tmp_path / uid / 'candidate', 8041, 'validation')
    return {'uid': uid, 'task': uid, 'reference': ref, 'candidate': candidate}


@pytest.mark.parametrize('a,b,accepted,reason', [(1, 1, True, 'non_regression_tie'),
    (0, 0, True, 'non_regression_tie'), (0, 1, True, 'non_regression_with_gain'), (1, 0, False, 'regression')])
def test_ties_and_gain_accepted_but_not_claimed_proven(tmp_path, a, b, accepted, reason):
    r = gate.decide([comparison(tmp_path, 'a', a, b)])
    assert r['accept'] is accepted and r['reason'] == reason
    assert r['effectiveness_proven'] is False


def test_one_case_gain_cannot_offset_regression(tmp_path):
    r = gate.decide([comparison(tmp_path, 'a', 0, 1), comparison(tmp_path, 'b', 1, 0)])
    assert not r['accept'] and r['reason'] == 'regression'


@pytest.mark.parametrize('field,value', [('reward', None), ('reward', float('nan')), ('reward', True),
    ('status', 'blocked'), ('uid', 'future'), ('purpose', 'stream')])
def test_invalid_candidate_not_treated_as_zero(tmp_path, field, value):
    p = comparison(tmp_path, 'a')
    p['candidate'][field] = value
    with pytest.raises(RuntimeError):
        gate.decide([p])


def test_native_artifact_tampering_is_rejected(tmp_path):
    p = comparison(tmp_path, 'a')
    atomic_json(Path(p['reference']['result']), {'tampered': True})
    with pytest.raises(RuntimeError, match='recorded_evidence_changed'):
        gate.decide([p])


def test_step_tie_admission_and_no_parent_replay(protocol):
    root, m = protocol
    native = Native()
    a = step(root, m, 0, native)
    assert a['reward'] == 1 and a['promoted'] and a['after']['revision'] == 1
    assert [(x[1], x[2]) for x in native.calls] == [('stream', 0), ('validation', 1)]
    b = step(root, m, 1, native)
    assert b['before'] == a['after'] and b['promoted']
    validation = [x for x in native.calls if x[1] == 'validation']
    assert len(validation) == 3 and all(Path(x[3]).name == 'candidate' for x in validation)
    assert [r['reward'] for r in (a, b)] == [1, 1]  # official scores not overwritten
    assert step(root, m, 0, native) == a and step(root, m, 1, native) == b
    assert len(native.calls) == 5
    decision = read_json(root / 'stream/s000001/validation/result.json')
    assert decision['parent_replays'] == 0
    assert decision['comparisons'][0]['same_parent_version'] is True
    assert decision['comparisons'][1]['same_parent_version'] is False  # historical retention check
    report = c.report(root, m)
    assert report['scored_tasks'] == 2 and report['active_skill_count'] == 1
    assert report['score_cohorts']['v2_formal_scores']['count'] == 2


def test_regression_preserves_previous_bank(protocol):
    root, m = protocol
    a = step(root, m, 0, Native())
    b = step(root, m, 1, Native(validation_score=0))
    assert not b['promoted'] and b['after'] == a['after'] == read_json(root / 'bank/HEAD.json')


def test_crash_after_promotion_uses_saved_formal_and_candidate(protocol, monkeypatch):
    root, m = protocol
    native = Native()
    save = v1.save_record
    def crash(*args):
        raise RuntimeError('after commit')
    monkeypatch.setattr(v1, 'save_record', crash)
    with pytest.raises(RuntimeError, match='after commit'):
        step(root, m, 0, native)
    assert len(native.calls) == 2 and read_json(root / 'bank/HEAD.json')['revision'] == 1
    monkeypatch.setattr(v1, 'save_record', save)
    record = step(root, m, 0, native)
    assert record['promoted'] and len(native.calls) == 2


def test_invalid_cached_cohort_and_decision_rejected(protocol):
    root, m = protocol
    native = Native()
    step(root, m, 0, native)
    f = root / 'stream/s000000'
    e = m['stream'][0]
    refs = read_json(f / 'validation_references.json')
    args = (f / 'validation', [e], refs, f / 'parent', read_json(f / 'parent/HEAD.json'),
            f / 'candidate', read_json(f / 'candidate/HEAD.json'), m['ports'], native)
    result = read_json(f / 'validation/result.json')
    result['accept'] = False
    atomic_json(f / 'validation/result.json', result)
    with pytest.raises(RuntimeError, match='cached_validation_decision_changed'):
        gate.evaluate(*args)


def test_wrong_parent_score_cannot_be_used_as_current_reference(protocol):
    root, m = protocol
    step(root, m, 0, Native())
    f = root / 'stream/s000000'
    refs = read_json(f / 'validation_references.json')
    refs['s000000']['before'] = {'revision': 999}
    with pytest.raises(RuntimeError, match='current_official_score_did_not_use_parent'):
        gate.evaluate(f / 'other', [m['stream'][0]], refs, f / 'parent', read_json(f / 'parent/HEAD.json'),
                      f / 'candidate', read_json(f / 'candidate/HEAD.json'), m['ports'], Native())


def make_boundary(protocol, monkeypatch):
    old_root, m = protocol
    m = copy.deepcopy(m)
    m['schema'] = 'cross-chain-self-skill-v1'
    del m['migration']
    m['gate'] = {'repeats': 1}
    v1.step(old_root, m, 0, solve=Native(), proposer=proposer, checker=lambda _: None)
    with pytest.raises(RuntimeError, match='cancelled redundant parent'):
        v1.step(old_root, m, 1, solve=Native(fail_parent=True), proposer=proposer, checker=lambda _: None)
    atomic_json(old_root / 'manifest.json', m)
    atomic_json(old_root / 'seal.json', {'test': 'immutable'})
    (old_root / 'PAUSE').write_text('authorized handoff')
    monkeypatch.setattr(v1, 'verify', lambda _: m)
    return old_root, m


def test_migration_preserves_real_history_and_never_resolves_prefix(protocol, monkeypatch):
    old_root, _ = make_boundary(protocol, monkeypatch)
    old_scores = [read_json(old_root / 'stream' / f's{i:06d}' / 'scored.json') for i in (0, 1)]
    old_record_hash = file_hash(old_root / 'stream/s000000/record.json')
    new_root = old_root.parent / 'v2'
    m = c.migrate(new_root, old_root, auditor=gate.validate_record, stopped=lambda _: None)
    assert m['migration']['official_prefix_scored_under_v1'] == 2
    assert m['migration']['first_new_protocol_solve_uid'] == 's000002'
    assert c.verify(new_root) == m
    assert c.migrate(new_root, old_root, auditor=gate.validate_record, stopped=lambda _: None) == m
    assert file_hash(new_root / 'stream/s000000/record.json') == old_record_hash
    def never(*a, **kw):
        pytest.fail('migration replayed an existing native solve')
    imported = step(new_root, m, 0, never)
    assert not imported['promoted'] and imported['after']['revision'] == 0
    admitted = step(new_root, m, 1, never)
    assert admitted['promoted'] and admitted['before']['revision'] == 0 and admitted['after']['revision'] == 1
    assert admitted['solve_protocol'] == 'cross-chain-self-skill-v1' and admitted['update_protocol'] == c.PROTOCOL
    assert c.verify(new_root) == m  # immutable cloned evidence has not changed
    for i, old in enumerate(old_scores):
        assert read_json(new_root / 'stream' / f's{i:06d}' / 'scored.json') == old
    native = Native()
    third = step(new_root, m, 2, native)
    assert third['before']['revision'] == 1 and third['solve_protocol'] == c.PROTOCOL
    assert native.calls[0][:3] == ('s000002', 'stream', 1)
    r = c.report(new_root, m)
    assert r['score_cohorts']['historical_v1_formal_scores']['count'] == 2
    assert r['score_cohorts']['v2_formal_scores']['count'] == 1
    assert file_hash(old_root / 'stream/s000000/record.json') == old_record_hash


def test_migration_copied_history_is_also_pinned(protocol, monkeypatch):
    old, _ = make_boundary(protocol, monkeypatch)
    new = old.parent / 'v2'
    c.migrate(new, old, auditor=gate.validate_record, stopped=lambda _: None)
    atomic_json(new / 'stream/s000000/source.json', {'fake': 'future experience'})
    with pytest.raises(RuntimeError, match='recorded_evidence_changed'):
        c.verify(new)


def test_migration_requires_finished_candidate_not_resampling(protocol, monkeypatch):
    old, _ = make_boundary(protocol, monkeypatch)
    target = old / 'stream/s000001/validation/s000001/00/candidate/record.json'
    target.rename(target.with_suffix('.saved'))
    with pytest.raises(RuntimeError, match='candidate_validation_not_complete'):
        c.migrate(old.parent / 'v2', old, auditor=gate.validate_record, stopped=lambda _: None)


def test_boundary_guard_does_not_retroactively_apply_new_memory(protocol, monkeypatch):
    old, _ = make_boundary(protocol, monkeypatch)
    before = read_json(old / 'bank/HEAD.json')
    bank.promote(old / 'bank', before, {'json': DOC}, {'json': {}}, 'unregistered')
    with pytest.raises(RuntimeError, match='cannot_rewrite_boundary_memory_history'):
        c.migrate(old.parent / 'v2', old, auditor=gate.validate_record, stopped=lambda _: None)


def test_later_work_cannot_be_discarded_during_migration(protocol, monkeypatch):
    old, _ = make_boundary(protocol, monkeypatch)
    atomic_json(old / 'stream/s000002/solve/launch.json', {'already': 'started'})
    with pytest.raises(RuntimeError, match='later_stream_work_exists'):
        c.migrate(old.parent / 'v2', old, auditor=gate.validate_record, stopped=lambda _: None)


def test_quarantined_stage_not_retried(protocol):
    root, m = protocol
    atomic_json(root / 'stream/s000000/blocked.json', {'reason': 'infrastructure'})
    native = Native()
    with pytest.raises(RuntimeError, match='stage_quarantined'):
        step(root, m, 0, native)
    assert not native.calls
