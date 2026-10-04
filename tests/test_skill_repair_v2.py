import copy
import json
import time
from pathlib import Path

import pytest

from skill_repair_v2 import learning as l, campaign as c
from rsi_harness import meta_skill as meta
from rsi_harness.clean_rsi import load_payload, public_trace, read_json
from rsi_harness.terminal_infra import atomic_json


def candidate(key='validate-layout'):
    return {'id': key, 'title': 'Validate board layout before search', 'level': 'task',
        'when': 'Before searching a board supplied as FEN, if parsing fails',
        'mechanism': 'A successful parse establishes syntactic validity, not visual accuracy',
        'steps': ['Count expanded squares on each rank', 'Parse and compare with the supplied board'],
        'cautions': ['Do not invent a missing piece or claim the move is correct solely from parsing'],
        'evidence_ids': ['e1'], 'transfer_check': 'Parse the current board and inspect its representation'}


def replies(accept=True):
    return [
        {'summary': 'Observed a board parse error, not a demonstrated cause of final failure',
         'observations': [{'id': 'e1', 'span_id': 't0001', 'interpretation': 'Parser rejected rank width',
                           'status': 'observed', 'resolution': 'unknown'}]},
        {'reason': 'Check syntactic board validity', 'upserts': [candidate()]},
        {'decisions': [{'id': 'validate-layout', 'accept': accept, 'reason': 'Narrow check' if accept else 'Needs narrowing',
                       'unsupported_claims': [] if accept else ['Overbroad mechanism']}]}]


class Transport:
    def __init__(self, values):
        self.values, self.requests = values, []

    def __call__(self, url, request, timeout):
        assert 0 < timeout <= 1800
        self.requests.append(copy.deepcopy(request))
        response = self.values[len(self.requests) - 1]
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(response)}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


@pytest.fixture
def source(tmp_path):
    trial = tmp_path / 'old'
    atomic_json(trial / 'agent/trajectory.json', {'steps': [
        {'source': 'agent', 'message': 'x' * 480 + 'ValueError: expected 8 columns on a board rank',
         'reasoning_content': 'PRIVATE_CANARY'}, {'source': 'user', 'message': 'USER_CANARY'}]})
    atomic_json(trial / 'verifier/ctrf.json', {'secret': 'GRADER_CANARY'})
    old = meta.proposal_check({'reason': 'Existing procedure', 'upserts': [candidate('old-check')]},
                             {'observations': [{'id': 'e1'}]}, {})
    return {'instruction': 'Find a move on a board represented as FEN', 'score': 0,
            'trace': public_trace(trial), 'current_skills': old}


def test_per_task_exact_evidence_preserved_and_incremental(tmp_path, source):
    transport = Transport(replies())
    folder = tmp_path / 'learn'
    result = l.learn(folder, source, 'frozen-model', 'http://unused', transport)
    assert result['status'] == 'complete' and result['outcome'] == 'candidate_ready'
    assert result['changed_ids'] == ['validate-layout']
    payload = load_payload(folder / 'state', result['version'])
    assert payload['old-check'] == source['current_skills']['old-check']
    evidence = read_json(folder / 'evidence.json')['observations'][0]
    assert evidence['quote'] == source['trace'][evidence['start']:evidence['end']]
    assert 'expected 8 columns' in evidence['quote']
    assert all(source['trace'][480:] in req['messages'][1]['content'] for req in transport.requests)
    assert not any(x in str(transport.requests) for x in ('GRADER_CANARY', 'PRIVATE_CANARY', 'USER_CANARY'))
    assert l.learn(folder, source, 'frozen-model', 'http://unused', transport) == result
    assert len(transport.requests) == 3


def test_one_bounded_revision_not_silent_deletion(tmp_path, source):
    response = replies(False)
    response += [replies()[1], replies()[2]]
    t = Transport(response)
    result = l.learn(tmp_path / 'learn', source, 'm', 'http://unused', t)
    assert result['changed_ids'] == ['validate-layout'] and len(t.requests) == 5
    assert result['accounting']['calls'] == 5


def test_all_rejected_not_force_accepted(tmp_path, source):
    response = replies(False) + [replies()[1], replies(False)[2]]
    result = l.learn(tmp_path / 'learn', source, 'm', 'http://unused', Transport(response))
    assert result['changed_ids'] == [] and result['outcome'] == 'no_retrievable_change'
    assert load_payload(tmp_path / 'learn/state', result['version']) == source['current_skills']


def test_interrupted_request_not_resampled(tmp_path, source):
    calls = []
    def crash(*args):
        calls.append(args)
        raise TimeoutError('network interruption')
    with pytest.raises(TimeoutError):
        l.learn(tmp_path / 'learn', source, 'm', 'http://unused', crash)
    with pytest.raises(RuntimeError, match='interrupted_request_not_repeated'):
        l.learn(tmp_path / 'learn', source, 'm', 'http://unused', crash)
    assert len(calls) == 1


def test_invalid_or_other_task_evidence_fails(tmp_path, source):
    responses = replies()
    responses[1]['upserts'][0]['evidence_ids'] = ['some-other-task/e1']
    with pytest.raises(ValueError, match='unknown evidence'):
        l.learn(tmp_path / 'learn', source, 'm', 'http://unused', Transport(responses))


def test_no_global_trace_thinning(tmp_path, source):
    source['trace'] = 'x' * 22080 + 'error with precise original text' + 'z' * 1000
    response = replies()
    response[0]['observations'][0]['span_id'] = 't0046'
    l.learn(tmp_path / 'learn', source, 'm', 'http://unused', Transport(response))
    ev = read_json(tmp_path / 'learn/evidence.json')['observations'][0]
    assert ev['start'] == 22080 and ev['quote'].startswith('error with precise original text')


def test_frozen_bank_mutation_detected(tmp_path, source):
    folder = tmp_path / 'learn'
    l.learn(folder, source, 'm', 'http://unused', Transport(replies()))
    frozen = l.freeze_bank(folder)
    assert l.verify_bank(folder, frozen)
    source['trace'] += 'mutation'
    atomic_json(folder / 'source.json', source)
    with pytest.raises(RuntimeError, match='frozen_candidate_changed'):
        l.verify_bank(folder, frozen)


def test_all_learning_must_finish_before_replay(tmp_path, source):
    m = {'rows': [{'task': 'one'}, {'task': 'two'}]}
    l.learn(tmp_path / 'tasks/one/learn', source, 'm', 'http://unused', Transport(replies()))
    assert c.queue(tmp_path, m) == [{'task': 'two', 'phase': 'learn'}]
    with pytest.raises(RuntimeError, match='freeze_before_all_learning_complete'):
        c.freeze_all(tmp_path, m)


def test_no_support_skipped_and_infra_not_scored_as_zero(tmp_path):
    m = {'rows': [{'task': 'one', 'old_reward': 0}, {'task': 'two', 'old_reward': 0}],
         'excluded': [{'reason': 'already_passed'}], 'source_total': 3, 'caveat': 'seen tasks'}
    atomic_json(tmp_path / 'manifest.json', m)
    atomic_json(tmp_path / 'tasks/one/learn/result.json', {'status': 'complete', 'outcome': 'no_retrievable_change'})
    atomic_json(tmp_path / 'tasks/two/learn/result.json', {'status': 'blocked', 'reason': 'timeout'})
    assert c.queue(tmp_path, m) == []
    report = c.report(tmp_path, m)
    assert report['replays_complete'] == 0 and report['repair_rate_among_scored_replays'] is None
    assert len(report['blocked']) == 1 and report['old_successes_not_replayed'] == 1


def test_audit_absence_is_not_evidence(tmp_path):
    evidence = [{'id': 'e1', 'quote': 'old error'}]
    reply = {'summary': 'No evidence', 'observations': [{'old_id': 'e1', 'verdict': 'avoided_or_repaired',
                                                        'reason': 'No error seen', 'new_span_ids': []}]}
    with pytest.raises(ValueError, match='ungrounded_repair_audit'):
        l.audit(tmp_path, evidence, 'A different command succeeded', 'm', 'http://unused', Transport([reply]))


def test_audit_cites_literal_new_evidence(tmp_path):
    evidence = [{'id': 'e1', 'quote': 'old error'}]
    trace = 'Board parser successfully accepted all eight ranks.'
    reply = {'summary': 'Syntax repaired, no conclusion about solving', 'observations': [
        {'old_id': 'e1', 'verdict': 'avoided_or_repaired', 'reason': 'Parser accepted the board', 'new_span_ids': ['t0000']}]}
    result = l.audit(tmp_path, evidence, trace, 'm', 'http://unused', Transport([reply]))
    assert result['observations'][0]['anchors'][0]['quote'] == trace


def test_replay_uses_600_verifier_and_preserves_old_module(tmp_path, monkeypatch):
    row = {'task': 'one', 'old_reward': 0, 'readiness': {'image_id': 'image'}}
    frozen = {'banks': {'one': {}}}
    monkeypatch.setattr(l, 'verify_bank', lambda *a: {'revision': 1})
    monkeypatch.setattr(c.core, 'ready', lambda *a: (True, None))
    monkeypatch.setattr(c, 'readiness', lambda *a: {'image_id': 'image'})
    observed = []
    def solve(*args):
        observed.append(c.cross.VERIFIER_TIMEOUT)
        raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(c.cross, 'execute_trial', solve)
    previous = c.cross.VERIFIER_TIMEOUT
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        c.execute_replay(tmp_path, row, 8040, frozen)
    assert observed == [600] and c.cross.VERIFIER_TIMEOUT == previous


def test_completed_worker_does_not_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'verify', lambda root: {'rows': [{'task': 'one'}], 'ports': [8040]})
    monkeypatch.setattr(c.core, 'PROJECT', tmp_path)
    path = tmp_path / 'tasks/one/replay/result.json'
    atomic_json(path, {'status': 'complete', 'reward': 1})
    monkeypatch.setattr(c, 'execute_replay', lambda *a: pytest.fail('completed solve repeated'))
    c.worker(tmp_path, 'one', 'replay', 8040)
    assert read_json(path)['reward'] == 1
