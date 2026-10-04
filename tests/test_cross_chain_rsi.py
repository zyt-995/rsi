import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cross_chain_rsi import memory as m, campaign as c
from rsi_harness.clean_rsi import initialize, read_json, load_payload
from rsi_harness.terminal_infra import atomic_json

DOC = '# JSON validation\n## When\nProducing JSON.\n## Steps\nParse it and check the required schema.\n## Cautions\nDo not require JSON for plain-text outputs.'


def replies(arm):
    review = {'decisions': [{'id': 'json-check', 'accept': True, 'reason': 'Bounded by JSON applicability', 'unsupported_claims': []}]}
    if arm == 'skill-rsi':
        return ({'summary': 'Reusable JSON checks', 'items': ['Check parsing and schema separately']},
                {'reason': 'Consolidation', 'skills': [{'id': 'json-check', 'title': 'JSON checks', 'when': 'Producing JSON',
                    'steps': ['Parse and check current schema'], 'cautions': ['Not for plain text']}]}, review)
    return ({'summary': 'Public JSON check observed; no transfer proof', 'observations': [
                {'id': 'e1', 'span_id': 't0000', 'interpretation': 'JSON validation observed', 'status': 'observed', 'resolution': 'not_applicable'}]},
            {'reason': 'Bounded mechanism', 'upserts': [{'id': 'json-check', 'title': 'JSON checks', 'level': 'meta',
                'when': 'Producing JSON', 'mechanism': 'Parsing and schema validation are distinct', 'steps': ['Parse then check current required values'],
                'cautions': ['Not for plain text'], 'evidence_ids': ['e1'], 'transfer_check': 'Assert current schema when applicable'}]}, review)


class Transport:
    def __init__(self, arm='skill-rsi', fail=None):
        self.replies = replies(arm)
        self.calls, self.fail = [], fail

    def __call__(self, base, request, timeout):
        self.calls.append(copy.deepcopy(request))
        if self.fail:
            raise self.fail
        return {'choices': [{'message': {'content': json.dumps(self.replies[len(self.calls)-1])}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


SOURCE = {'trace': 'Public JSON check succeeded; keys and values asserted.', 'tasks': [], 'libraries': []}


@pytest.mark.parametrize('arm', m.ARMS)
def test_consolidation_freeze_and_no_resampling(tmp_path, arm):
    bank = tmp_path / arm
    t = Transport(arm)
    r = m.consolidate(bank, arm, SOURCE, 'http://unused', t)
    assert r['accepted'] and r['after']['revision'] == 1 and r['accounting']['calls'] == 3
    assert [x['max_tokens'] for x in t.calls] == [2048, 4096, 2048]
    assert m.consolidate(bank, arm, SOURCE, 'http://unused', t) == r
    assert len(t.calls) == 3
    frozen = m.freeze(bank)
    assert m.verify_frozen(bank) == frozen == m.freeze(bank)
    assert set(load_payload(bank / 'state', frozen['version'])) == {'json-check'}


def test_frozen_head_tampering_fails_closed(tmp_path):
    m.consolidate(tmp_path, 'skill-rsi', SOURCE, 'unused', Transport())
    m.freeze(tmp_path)
    atomic_json(tmp_path / 'state/HEAD.json', {'corrupt': True})
    with pytest.raises(ValueError):
        m.verify_frozen(tmp_path)


def test_failed_request_counted_not_resampled(tmp_path):
    t = Transport(fail=TimeoutError('timeout'))
    r = m.consolidate(tmp_path, 'skill-rsi', SOURCE, 'unused', t)
    assert not r['accepted'] and r['accounting']['calls'] == 1
    assert not r['accounting']['usage_complete']
    assert m.consolidate(tmp_path, 'skill-rsi', SOURCE, 'unused', Transport()) == r
    assert m.freeze(tmp_path)['version']['revision'] == 0


def test_interrupted_request_not_resampled(tmp_path):
    class Crash(BaseException):
        pass
    with pytest.raises(Crash):
        m.consolidate(tmp_path, 'skill-rsi', SOURCE, 'unused', Transport(fail=Crash()))
    t = Transport()
    r = m.consolidate(tmp_path, 'skill-rsi', SOURCE, 'unused', t)
    assert 'interrupted_request_not_repeated' in r['reason'] and not t.calls


def test_unknown_evidence_and_oversized_library_rejected():
    diagnosis, proposal, _ = replies('meta-skill-rsi')
    proposal['upserts'][0]['evidence_ids'] = ['nonexistent']
    with pytest.raises(ValueError):
        m.proposal_docs('meta-skill-rsi', proposal, diagnosis)
    with pytest.raises(ValueError):
        m.proposal_docs('skill-rsi', {'reason': 'bad', 'skills': [{'id': str(i), 'markdown': DOC} for i in range(17)]}, {})


def test_unsupported_review_never_commits(tmp_path):
    t = Transport()
    t.replies[-1]['decisions'][0]['unsupported_claims'] = ['unproven universality']
    r = m.consolidate(tmp_path, 'skill-rsi', SOURCE, 'unused', t)
    assert not r['accepted'] and r['after']['revision'] == 0


def test_collect_only_public_source_bounded_deterministic(tmp_path):
    entries = []
    for i in range(19):
        folder = tmp_path / str(i)
        initialize(folder / 'state', 'skill-rsi')
        atomic_json(folder / 'trial/agent/trajectory.json', {'steps': [
            {'source': 'agent', 'message': ('PUBLIC %s ' % i) * 6000, 'reasoning_content': 'PRIVATE_CANARY'},
            {'source': 'user', 'message': 'USER_CANARY'}]})
        atomic_json(folder / 'trial/verifier/ctrf.json', {'hidden': 'GRADER_CANARY'})
        atomic_json(folder / 'trial/result.json', {})
        atomic_json(folder / 'record.json', {'status': 'complete', 'reward': 0, 'result': str(folder / 'trial/result.json')})
        instruction = folder / 'instruction.md'
        instruction.write_text('Public instruction ' * 300)
        entries.append({'chain': str(i), 'store': str(folder / 'state'), 'tasks': [
            {'task': str(i), 'record': str(folder / 'record.json'), 'instruction': str(instruction)}]})
    one, audit = m.collect_sources(entries)
    assert m.collect_sources(entries) == (one, audit)
    assert len(one['trace']) <= 24000 and len(one['tasks']) == 19
    assert not any(s in json.dumps(one) for s in ('PRIVATE_CANARY', 'USER_CANARY', 'GRADER_CANARY'))
    assert all(len(t['instruction']) <= 1600 for t in one['tasks'])
    assert all('verifier' not in p for p in audit)
    atomic_json(Path(entries[0]['tasks'][0]['record']), {'status': 'blocked'})
    with pytest.raises(ValueError):
        m.collect_sources(entries)


def manifest(tmp_path):
    return {'source_order': ['s'], 'source_tasks': {'s': ['s', 's-EVO-1']},
            'source_roots': {a: str(tmp_path / a) for a in m.ARMS},
            'target_order': ['a', 'b'], 'target_tasks': {'a': ['a', 'a-EVO-1'], 'b': ['b', 'b-EVO-1']}, 'interpretation': 'test'}


def test_source_gate_and_zero_is_complete(tmp_path):
    mf = manifest(tmp_path)
    arm = 'meta-skill-rsi'
    p = c.record_path(tmp_path / arm, 's', arm, 's')
    atomic_json(p, {'status': 'complete', 'reward': 0})
    progress = c.source_progress(mf, arm)
    assert len(progress['complete']) == 1 and progress['next'] == [{'chain': 's', 'task': 's-EVO-1'}]
    assert c.candidates(tmp_path, mf) == [{'kind': 'source', 'arm': arm, 'chain': 's', 'task': 's-EVO-1'}]
    atomic_json(c.record_path(tmp_path / arm, 's', arm, 's-EVO-1'), {'status': 'blocked', 'reason': 'infra'})
    assert c.candidates(tmp_path, mf) == []
    assert not c.banks_frozen(tmp_path)


def test_same_task_order_and_paired_metrics(tmp_path):
    mf = manifest(tmp_path)
    _, _, queue = c.target_scan(tmp_path, mf)
    assert [x['task'] for x in queue[:3]] == ['a'] * 3
    atomic_json(c.record_path(tmp_path, 'a', 'baseline', 'a'), {'status': 'complete', 'reward': 1})
    _, _, queue = c.target_scan(tmp_path, mf)
    assert [(x['arm'], x['task']) for x in queue[:2]] == [('skill-rsi', 'a'), ('meta-skill-rsi', 'a')]
    assert c.report(tmp_path, mf)['matched_task_ids'] == []
    for arm in m.ARMS:
        atomic_json(c.record_path(tmp_path, 'a', arm, 'a'), {'status': 'complete', 'reward': 0})
    stats = c.report(tmp_path, mf)
    assert stats['matched_task_ids'] == ['a'] and stats['target']['baseline']['matched_mean_reward'] == 1
    assert stats['target']['skill-rsi']['matched_mean_reward'] == 0
    assert stats['matched_full_chains'] == []


def test_invalid_result_blocks_chain_not_scored(tmp_path):
    mf = manifest(tmp_path)
    atomic_json(c.record_path(tmp_path, 'a', 'baseline', 'a'), {'status': 'blocked', 'reason': 'invalid_result'})
    done, blocked, queue = c.target_scan(tmp_path, mf)
    assert not done and len(blocked) == 1
    assert not any(x['arm'] == 'baseline' and x['chain'] == 'a' for x in queue)


def test_prebuilt_exemption_not_a_global_lfs_bypass(tmp_path, monkeypatch):
    task = 't'
    ticket = {'status': 'ready', 'image': 'image', 'image_id': 'sha256:known'}
    mf = {'target_images': {task: ticket}, 'asset_exemptions': {task: {}}}
    monkeypatch.setattr(c, 'readiness', lambda *a: ticket)
    monkeypatch.setattr(c, 'image_id', lambda *a: ticket['image_id'])
    monkeypatch.setattr(c.core, 'missing_assets', lambda *a: ['tests/test-data.bin'])
    assert c.target_ready(tmp_path, task, mf) == (False, 'unmaterialized_lfs:tests/test-data.bin')
    with pytest.raises(RuntimeError):
        c.prebuilt_asset_exemption('unrelated-task', ticket)


def test_stage_requires_both_banks_before_any_call(tmp_path, monkeypatch):
    mf = manifest(tmp_path)
    mf['ports'] = [8040]
    monkeypatch.setattr(c, 'verify', lambda r: mf)
    monkeypatch.setattr(c, 'PROJECT', tmp_path)
    with patch.object(c, 'execute_trial') as solve:
        with pytest.raises(RuntimeError, match='BOTH frozen'):
            c.stage(tmp_path, 'a', 'baseline', 'a', 8040)
        solve.assert_not_called()


def test_interrupted_target_not_resampled(tmp_path):
    atomic_json(tmp_path / 'launch.json', {'started': 1})
    with patch.object(c.subprocess, 'Popen') as process:
        with pytest.raises(RuntimeError, match='interrupted_trial_not_repeated'):
            c.execute_trial(tmp_path, Path('/unused'), 'baseline', tmp_path / 'state', 8040, None)
        process.assert_not_called()


def test_saved_target_is_consumed_without_second_solve(tmp_path, monkeypatch):
    folder = tmp_path / 'stage'
    trial = folder / 'trials/one'
    atomic_json(folder / 'launch.json', {'started': 1})
    atomic_json(trial / 'result.json', {'verifier_result': {'rewards': {'reward': 0}}, 'agent_result': {}})
    atomic_json(trial / 'agent/rsi_start.json', {'arm': 'baseline', 'version': None, 'selected_skills': []})
    monkeypatch.setattr(c.admission, 'scored_result', lambda *a: (True, None))
    with patch.object(c.subprocess, 'Popen') as process:
        r = c.execute_trial(folder, Path('/unused/task'), 'baseline', tmp_path / 'unused', 8040, None)
        assert r['status'] == 'complete' and r['reward'] == 0 and not r['target_learning']
        process.assert_not_called()


def test_completed_target_stage_is_idempotent(tmp_path, monkeypatch):
    mf = manifest(tmp_path)
    mf['ports'] = [8040]
    monkeypatch.setattr(c, 'verify', lambda r: mf)
    monkeypatch.setattr(c, 'PROJECT', tmp_path)
    path = c.record_path(tmp_path, 'a', 'skill-rsi', 'a')
    atomic_json(path, {'status': 'complete', 'reward': 0})
    before = path.read_bytes()
    with patch.object(c, 'execute_trial') as solve:
        c.stage(tmp_path, 'a', 'skill-rsi', 'a', 8040)
        solve.assert_not_called()
    assert path.read_bytes() == before


def test_identical_skill_ids_in_two_banks_never_share_state(tmp_path):
    for arm in m.ARMS:
        m.consolidate(tmp_path / arm, arm, SOURCE, 'unused', Transport(arm))
        m.freeze(tmp_path / arm)
    ordinary = m.verify_frozen(tmp_path / 'skill-rsi')
    meta = m.verify_frozen(tmp_path / 'meta-skill-rsi')
    assert ordinary['payload_sha256'] != meta['payload_sha256']
    atomic_json(tmp_path / 'meta-skill-rsi/state/HEAD.json', {'changed': True})
    assert m.verify_frozen(tmp_path / 'skill-rsi') == ordinary
    with pytest.raises(ValueError):
        m.verify_frozen(tmp_path / 'meta-skill-rsi')
