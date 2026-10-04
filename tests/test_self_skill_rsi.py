"""Offline protocol tests: no model requests, Docker jobs or legacy mutations."""
import copy
import json
import time
from pathlib import Path

import pytest

from rsi_harness.clean_rsi import digest, initialize, load_payload, public_trace, read_json, select_skills
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi import bank, campaign, gate, learning, runtime


DOC = '# JSON validation\n## When\nProducing JSON.\n## Steps\nParse and check the current schema.\n## Cautions\nNot for plain text.'


def skill(key='json-check', uid='s000000', revised=False):
    return {'id': key, 'title': 'Validate JSON', 'level': 'task',
            'when': 'Before submitting a JSON artifact',
            'mechanism': 'Syntax and schema are distinct checks',
            'steps': ['Inspect the required schema', 'Parse the current artifact',
                      'Check required keys' if not revised else 'Also check nested field types'],
            'cautions': ['Does not establish semantic correctness; not for plain text'],
            'evidence_ids': [uid + ':e1'], 'transfer_check': 'Parse and check the current public schema'}


def replies(uid='s000000', *, accepted=True, revised=False):
    return [
        {'summary': 'Parser rejected an artifact; final failure cause is unproven',
         'observations': [{'id': 'e1', 'span_id': 't0000', 'interpretation': 'Public JSON parser failed',
                           'status': 'observed', 'resolution': 'unknown'}]},
        {'reason': 'Scoped public validation procedure', 'upserts': [skill(uid=uid, revised=revised)], 'retire': []},
        {'accept': accepted, 'reason': 'Scoped hypothesis' if accepted else 'Needs narrowing',
         'unsupported_claims': [] if accepted else ['Universality not justified']}]


class Transport:
    def __init__(self, values):
        self.values, self.requests = copy.deepcopy(values), []

    def __call__(self, url, request, timeout):
        assert 0 < timeout <= 1800
        self.requests.append(copy.deepcopy(request))
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.values[len(self.requests) - 1])}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


@pytest.fixture
def source(tmp_path):
    trial = tmp_path / 'source-trial'
    atomic_json(trial / 'agent/trajectory.json', {'steps': [
        {'source': 'agent', 'message': 'Public JSON parser failed with a syntax error.',
         'reasoning_content': 'PRIVATE_CANARY'}, {'source': 'user', 'message': 'USER_CANARY'}]})
    atomic_json(trial / 'verifier/ctrf.json', {'answer': 'GRADER_CANARY'})
    return {'uid': 's000000', 'chain': 'a', 'task': 'a', 'instruction': 'Produce JSON',
            'trace': public_trace(trial), 'score': 0, 'consumed_version': {'revision': 0}}


def test_persistent_bank_and_legacy_solver_compatibility(tmp_path):
    store = tmp_path / 'bank'
    zero = bank.initialize(store)
    docs = {f'json-{n:02d}': DOC for n in range(64)}
    provenance = {k: {'origin': 'test'} for k in docs}
    one = bank.promote(store, zero, docs, provenance, 's000000')
    assert initialize(store, 'skill-rsi') == one
    assert load_payload(store, one) == docs
    assert len(select_skills(docs, 'JSON')[0]) == 4
    assert bank.initialize(store) == one  # NOT empty at a chain boundary
    assert bank.load(store, zero) == ({}, {})
    assert bank.promote(store, zero, docs, provenance, 's000000') == one  # commit recovery
    with pytest.raises(RuntimeError, match='stale_update_parent'):
        bank.promote(store, zero, {'other': DOC}, {'other': {}}, 'stale')
    with pytest.raises(ValueError, match='immutable_version_collision'):
        bank.publish(store, {}, {}, 1, 'overwrite')
    restored = bank.restore(store, one, 0, 'explicit offline rollback')
    assert restored['revision'] == 2 and bank.load(store)[0] == {}
    assert bank.load(store, one)[0] == docs


@pytest.mark.parametrize('payload', [
    {f's{i}': DOC for i in range(65)}, {'../unsafe': DOC}, {'a': 'no sections'},
    {'a': DOC + 'x' * 6000}, {f's{i}': DOC + 'x' * 5000 for i in range(30)}])
def test_library_budgets_and_paths(payload):
    with pytest.raises(ValueError):
        bank.validate(payload)


def test_bank_markdown_and_provenance_integrity(tmp_path):
    h = bank.publish(tmp_path, {'check': DOC}, {'check': {}}, 0, 'test')
    (tmp_path / 'versions/000000/skills/check.md').write_text('tamper')
    with pytest.raises(RuntimeError, match='skill_markdown_corrupted'):
        bank.load(tmp_path, h)
    (tmp_path / 'versions/000000/skills/check.md').write_text(DOC)
    atomic_json(tmp_path / 'versions/000000/provenance.json', {'check': {'tampered': True}})
    with pytest.raises(RuntimeError, match='skill_version_corrupted'):
        bank.load(tmp_path, h)


def test_same_model_grounded_learning_preserves_old_and_resume(tmp_path, source):
    t = Transport(replies())
    old, provenance = {'unrelated': DOC}, {'unrelated': {'origin': 'earlier'}}
    result = learning.propose(tmp_path / 'update', source, old, provenance, [], 'frozen-model', 'unused', t)
    assert result['status'] == 'candidate'
    assert result['payload']['unrelated'] == DOC
    assert result['provenance']['unrelated'] == provenance['unrelated']
    e = result['diagnosis']['observations'][0]
    assert e['id'] == 's000000:e1' and e['source_uid'] == source['uid']
    assert e['quote'] == source['trace'][e['start']:e['end']]
    assert {r['model'] for r in t.requests} == {'frozen-model'}
    assert [r['max_tokens'] for r in t.requests] == [3072, 8192, 3072]
    assert not any(s in str(t.requests) for s in ('PRIVATE_CANARY', 'USER_CANARY', 'GRADER_CANARY'))
    assert result['accounting']['known_tokens']['total_tokens'] == 90
    assert learning.propose(tmp_path / 'update', source, old, provenance, [], 'frozen-model', 'unused', t) == result
    assert len(t.requests) == 3
    with pytest.raises(RuntimeError, match='learning_inputs_changed'):
        learning.propose(tmp_path / 'update', {**source, 'score': 1}, old, provenance, [], 'm', 'unused', t)


def test_whole_skill_rewrite_merge_retire_preserve_unrelated():
    old = {'a': DOC, 'b': DOC, 'untouched': DOC}
    provenance = {k: {'origin': 'old'} for k in old}
    evidence = [{'id': 's000000:e1', 'quote': 'Exact public observation'}]
    patch = {'reason': 'Merge redundant checks', 'upserts': [skill('merged')],
             'retire': [{'id': k, 'reason': 'Covered conditionally', 'evidence_ids': ['s000000:e1'],
                         'replaced_by': ['merged']} for k in ('a', 'b')]}
    docs, lineage = learning.apply_patch(patch, old, provenance, evidence, old, 's000000')
    assert set(docs) == {'merged', 'untouched'} and docs['untouched'] == DOC
    assert lineage['merged']['evidence'] == evidence
    assert old['a'] == DOC and provenance['a'] == {'origin': 'old'}  # input never mutated
    rewrite = {'reason': 'Replace complete procedure', 'upserts': [skill('merged', revised=True)], 'retire': []}
    updated, _ = learning.apply_patch(rewrite, docs, lineage, evidence, docs, 's000001')
    assert updated['merged'] != docs['merged']
    assert 'nested field' in updated['merged']


@pytest.mark.parametrize('kind', ['unseen_edit', 'unknown_evidence', 'ungrounded_retire', 'unseen_retire', 'retired_merge', 'unknown_merge'])
def test_unfounded_or_unseen_edits_rejected(kind):
    old, provenance = {'a': DOC, 'b': DOC}, {'a': {}, 'b': {}}
    patch = {'reason': 'Evidence required', 'upserts': [], 'retire': []}
    editable = {'a'}
    if kind in ('unseen_edit', 'unknown_evidence'):
        item = skill('b' if kind == 'unseen_edit' else 'a')
        if kind == 'unknown_evidence':
            item['evidence_ids'] = ['future:e1']
        patch['upserts'] = [item]
    else:
        patch['retire'] = [{'id': 'b' if kind == 'unseen_retire' else 'a', 'reason': 'obsolete',
                            'evidence_ids': [] if kind == 'ungrounded_retire' else ['s000000:e1'],
                            'replaced_by': ['a'] if kind == 'retired_merge' else ['missing'] if kind == 'unknown_merge' else []}]
    with pytest.raises(ValueError):
        learning.apply_patch(patch, old, provenance, [{'id': 's000000:e1'}], editable, 's000000')


@pytest.mark.parametrize('final_accept', [False, True])
def test_one_bounded_revision_and_review_is_not_validation(tmp_path, source, final_accept):
    response = replies(accepted=False) + replies(accepted=final_accept, revised=True)[1:]
    t = Transport(response)
    r = learning.propose(tmp_path, source, {}, {}, [], 'same', 'unused', t)
    assert r['status'] == ('candidate' if final_accept else 'no_change')
    assert r['accounting']['calls'] == 5
    assert not (tmp_path / 'bank').exists()  # proposer cannot promote anything


def test_interrupted_model_request_never_repeated(tmp_path, source):
    calls = []
    def fail(*args):
        calls.append(True)
        raise TimeoutError('transport interrupted')
    with pytest.raises(TimeoutError):
        learning.propose(tmp_path, source, {}, {}, [], 'same', 'unused', fail)
    with pytest.raises(RuntimeError, match='interrupted_request_not_repeated'):
        learning.propose(tmp_path, source, {}, {}, [], 'same', 'unused', fail)
    assert len(calls) == 1
    assert learning.accounting(tmp_path)['usage_complete'] is False


def test_past_evidence_preserved_namespaced_not_globally_thinned(tmp_path, source):
    source['trace'] = 'x' * 22080 + 'JSON exact late parser error' + 'z' * 1000
    response = replies()
    response[0]['observations'][0]['span_id'] = 't0046'
    r = learning.propose(tmp_path, source, {}, {}, [], 'm', 'unused', Transport(response))
    e = r['diagnosis']['observations'][0]
    assert e['start'] == 22080 and e['quote'].startswith('JSON exact late parser error')
    next_source = {**source, 'uid': 's000001', 'chain': 'b'}
    d = copy.deepcopy(r['diagnosis'])
    d['observations'][0]['id'] = 's000001:e1'
    bundle, _ = learning.context(next_source, d, {}, [{'source': source, 'diagnosis': r['diagnosis']}])
    assert {x['id'] for x in bundle['OBSERVATIONS']} == {'s000000:e1', 's000001:e1'}
    assert bundle['PAST_SOURCES'][0]['observations'][0]['quote'] == e['quote']


def pair(uid='s000000', a=0, b=1, repeat=0):
    def result(score):
        return {'uid': uid, 'status': 'complete', 'purpose': 'validation', 'reward': score}
    return {'uid': uid, 'repeat': repeat, 'parent': result(a), 'candidate': result(b)}


@pytest.mark.parametrize('pairs,accept,reason', [
    ([pair()], True, 'paired_reward_gain_without_case_regression'),
    ([pair(), pair('guard', 1, 1)], True, 'paired_reward_gain_without_case_regression'),
    ([pair(), pair('guard', 1, 0)], False, 'regression'),
    ([pair(a=0, b=0)], False, 'no_measured_reward_gain'),
    ([pair(a=1, b=1)], False, 'no_measured_reward_gain')])
def test_native_gate_requires_gain_without_regression(pairs, accept, reason):
    d = gate.decide(pairs, 1)
    assert d['accept'] is accept and d['reason'] == reason


def test_gate_invalid_infrastructure_is_not_zero_and_repeats_exact():
    p = pair()
    p['candidate']['status'] = 'blocked'
    with pytest.raises(RuntimeError, match='not_algorithmic_failure'):
        gate.decide([p], 1)
    with pytest.raises(ValueError, match='incomplete_validation_repeats'):
        gate.decide([pair()], 2)
    with pytest.raises(ValueError, match='duplicate_validation_pair'):
        gate.decide([pair(), pair()], 1)
    assert gate.decide([pair(repeat=0), pair(repeat=1, a=1, b=1)], 2)['accept']


def test_fixed_historical_validation_prefers_other_chain_success():
    def entry(uid, chain):
        return {'uid': uid, 'chain': chain}
    records = [(entry('old-cross', 'b'), {'status': 'complete', 'purpose': 'stream', 'reward': 1}),
               (entry('recent-same', 'a'), {'status': 'complete', 'purpose': 'stream', 'reward': 1}),
               (entry('never-use-probe', 'c'), {'status': 'complete', 'purpose': 'probe', 'reward': 1})]
    current = entry('now', 'a')
    assert [e['uid'] for e in gate.select_cases(current, records)] == ['now', 'old-cross']
    assert gate.select_cases(current, []) == [current]


class FakeNative:
    """Idempotent complete trial records, public/private separation, no inference."""
    def __init__(self):
        self.calls = []

    def __call__(self, entry, store, head, folder, port, purpose):
        prior = read_json(folder / 'record.json')
        if prior:
            return prior
        self.calls.append((entry['uid'], purpose, head['revision']))
        payload, _ = bank.load(store, head)
        # v1 repairs the first failure; v2 breaks previously solved behavior.
        reward = (1 if head['revision'] == 1 else 0) if purpose == 'validation' else int(bool(payload))
        path = folder / 'trials/synthetic/result.json'
        atomic_json(path, {'synthetic': True, 'reward': reward})
        atomic_json(path.parent / 'agent/trajectory.json', {'steps': [
            {'source': 'agent', 'message': 'Public JSON parser failed with a syntax error.', 'reasoning_content': 'PRIVATE_CANARY'}]})
        atomic_json(path.parent / 'verifier/ctrf.json', {'private': 'GRADER_CANARY'})
        r = {'status': 'complete', 'uid': entry['uid'], 'task': entry['task'], 'purpose': purpose,
             'before': head, 'reward': reward, 'result': str(path), 'solve_seconds': 1,
             'hashes': {str(p): file_hash(p) for p in (path, path.parent / 'agent/trajectory.json')}}
        atomic_json(folder / 'record.json', r)
        return r


@pytest.fixture
def protocol(tmp_path):
    entries = []
    for i, chain in enumerate(('a', 'b', 'unseen')):
        task = tmp_path / 'tasks' / chain
        task.mkdir(parents=True)
        (task / 'instruction.md').write_text('Produce JSON with the current required schema')
        entries.append({'uid': ('s' if i < 2 else 'h') + f'{i:06d}', 'task': chain, 'chain': chain,
                        'version': 0, 'path': str(task)})
    m = {'stream': entries[:2], 'heldout': entries[2:], 'ports': [8040, 8041, 8042, 8044],
         'gate': {'repeats': 1}, 'model': 'same-frozen-model', 'request': {'chains': ['a', 'b']}, 'claims': 'synthetic'}
    root = tmp_path / 'campaign'
    bank.initialize(root / 'bank')
    return root, m


def run_step(root, m, index, fake):
    def proposer(folder, source, old, provenance, history, model, url):
        assert read_json(folder.parent / 'scored.json')  # score committed before updater
        assert [h['source']['uid'] for h in history] == [e['uid'] for e in m['stream'][:index]]
        assert all(h['source']['chain'] != 'unseen' for h in history)
        transport = Transport(replies(source['uid'], revised=index > 0))
        return learning.propose(folder, source, old, provenance, history, model, url, transport)
    return campaign.step(root, m, index, solve=fake, proposer=proposer, checker=lambda e: None)


def test_end_to_end_cross_chain_promotion_rejection_probe_and_no_duplicates(protocol):
    root, m = protocol
    fake = FakeNative()
    first = run_step(root, m, 0, fake)
    assert first['reward'] == 0 and first['promoted'] and first['after']['revision'] == 1
    second = run_step(root, m, 1, fake)
    assert second['before'] == first['after']  # cross-chain memory carried forward
    assert second['reward'] == 1 and not second['promoted'] and second['promotion_reason'] == 'regression'
    assert second['after'] == first['after']
    assert bank.load(root / 'bank')[0] == bank.load(root / 'stream/s000000/candidate')[0]
    old_calls = len(fake.calls)
    assert run_step(root, m, 0, fake) == first and run_step(root, m, 1, fake) == second
    assert len(fake.calls) == old_calls
    frozen = read_json(root / 'bank/HEAD.json')
    probes = campaign.probe(root, m, solve=fake, checker=lambda e: None)
    assert probes[0]['before'] == frozen and probes[0]['purpose'] == 'probe'
    assert read_json(root / 'bank/HEAD.json') == frozen
    report = campaign.report(root, m)
    assert report['scored_tasks'] == 2 and report['official_mean_reward'] == .5
    assert report['validation_solves_excluded_from_score'] == 6 and report['promotions'] == 1
    assert report['learning_calls'] == 6
    assert len(campaign.completed_prefix(root, m, 2)) == 2


def test_commit_crash_recovery_without_resolving(protocol, monkeypatch):
    root, m = protocol
    fake = FakeNative()
    original = campaign.save_record
    monkeypatch.setattr(campaign, 'save_record', lambda *a: (_ for _ in ()).throw(RuntimeError('crash after commit')))
    with pytest.raises(RuntimeError, match='crash after commit'):
        run_step(root, m, 0, fake)
    assert read_json(root / 'bank/HEAD.json')['revision'] == 1
    count = len(fake.calls)
    monkeypatch.setattr(campaign, 'save_record', original)
    recovered = run_step(root, m, 0, fake)
    assert recovered['promoted'] and len(fake.calls) == count
    assert len(list((root / 'bank/versions').glob('[0-9]*'))) == 2


def test_completed_evidence_tampering_and_out_of_order_fail(protocol):
    root, m = protocol
    fake = FakeNative()
    with pytest.raises(RuntimeError, match='predecessor_incomplete'):
        run_step(root, m, 1, fake)
    run_step(root, m, 0, fake)
    atomic_json(root / 'stream/s000000/update/evidence.json', {'corrupt': True})
    with pytest.raises(RuntimeError, match='recorded_evidence_changed'):
        run_step(root, m, 1, fake)


def test_quarantined_stage_not_automatically_retried(protocol):
    root, m = protocol
    atomic_json(root / 'stream/s000000/blocked.json', {'reason': 'infrastructure'})
    fake = FakeNative()
    with pytest.raises(RuntimeError, match='stage_quarantined'):
        run_step(root, m, 0, fake)
    assert fake.calls == []


def test_identical_retrieval_skips_costly_gate_and_pins_plan(protocol):
    root, m = protocol
    parent, candidate = root / 'parent', root / 'candidate'
    a, b = bank.initialize(parent), bank.publish(candidate, {}, {}, 1, 'same')
    fake = FakeNative()
    r = gate.evaluate(root / 'validation', m['stream'][:1], parent, a, candidate, b, m['ports'], 1, fake)
    assert not r['accept'] and not r['pairs'] and not fake.calls
    with pytest.raises(RuntimeError, match='validation_plan_changed'):
        gate.evaluate(root / 'validation', m['stream'][1:], parent, a, candidate, b, m['ports'], 1, fake)


def test_cached_gate_cannot_drop_guard_case(protocol):
    root, m = protocol
    a = bank.initialize(root / 'parent')
    b = bank.publish(root / 'candidate', {'json': DOC}, {'json': {}}, 1, 'test')
    f = root / 'validation'
    r = gate.evaluate(f, m['stream'], root / 'parent', a, root / 'candidate', b, m['ports'], 1, FakeNative())
    r['pairs'] = r['pairs'][:1]
    atomic_json(f / 'result.json', r)
    with pytest.raises(RuntimeError, match='cached_validation_cohort_changed'):
        gate.evaluate(f, m['stream'], root / 'parent', a, root / 'candidate', b, m['ports'], 1, FakeNative())


def test_old_controller_blocks_resource_lease_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'REPAIR_ROOT', tmp_path)
    atomic_json(tmp_path / 'status.json', {'pid': 1234, 'identity': 'live'})
    monkeypatch.setattr(runtime.core, 'process_identity', lambda pid: 'live' if pid == 1234 else None)
    before = file_hash(tmp_path / 'status.json')
    with pytest.raises(RuntimeError, match='existing_controller_or_watchdog_live'):
        with runtime.inference_lease([8040]):
            pytest.fail('occupied service leased')
    assert file_hash(tmp_path / 'status.json') == before
    assert not (tmp_path / 'PAUSE').exists()


def test_native_interruption_never_relaunched(protocol, monkeypatch):
    root, m = protocol
    e = {**m['stream'][0], 'task_checksum': 'pinned'}
    h = read_json(root / 'bank/HEAD.json')
    monkeypatch.setattr(runtime, 'check_entry', lambda entry: None)
    monkeypatch.setattr(runtime.core, 'docker_disk_status', lambda _: {'ok': True})
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **kw: pytest.fail('trial repeated'))
    folder = root / 'interrupted'
    atomic_json(folder / 'launch.json', {'started': time.time()})
    with pytest.raises(RuntimeError, match='interrupted_trial_not_repeated'):
        runtime.native_solve(e, root / 'bank', h, folder, 8040, 'stream')


def test_probe_cannot_precede_completed_learning_stream(protocol):
    root, m = protocol
    with pytest.raises(RuntimeError, match='predecessor_incomplete'):
        campaign.probe(root, m, solve=FakeNative(), checker=lambda e: None)


def test_validator_rejects_other_task_score():
    p = pair()
    p['candidate']['uid'] = 'future'
    with pytest.raises(RuntimeError, match='not_algorithmic_failure'):
        gate.decide([p], 1)


def test_prepare_has_no_model_calls_disjoint_cohorts_and_seal(tmp_path, monkeypatch):
    import harbor.models.task.task as task_module
    from types import SimpleNamespace
    monkeypatch.setattr(task_module, 'Task', lambda p: SimpleNamespace(checksum='checksum-' + p.name))
    monkeypatch.setattr(campaign.core, 'DATASET', tmp_path / 'tasks')
    monkeypatch.setattr(campaign.core, 'chains', lambda: ['a', 'b', 'h'])
    monkeypatch.setattr(campaign.core, 'tasks', lambda c: [c, c + '-EVO-1'])
    monkeypatch.setattr(campaign.core, 'ready', lambda *a: (True, None))
    monkeypatch.setattr(campaign, 'readiness', lambda *a: {'image': 'cached', 'image_id': 'sha256:pinned'})
    monkeypatch.setattr(campaign, 'fingerprint', lambda p: 'env-' + p.name)
    monkeypatch.setattr(campaign, 'snapshot', lambda: {'code': 'pinned'})
    config = {'schema': 'self-skill-rsi-config-v1', 'chains': ['a', 'b'], 'heldout_chains': ['h'],
              'ports': [8040, 8041], 'readiness_root': 'unchanged', 'cohort_role': 'development_seen', 'validation_repeats': 1}
    p = tmp_path / 'config.json'
    atomic_json(p, config)
    root = tmp_path / 'run'
    m = campaign.prepare(root, p)
    assert len(m['stream']) == 4 and len(m['heldout']) == 2
    assert [e['chain'] for e in m['stream']] == ['a', 'a', 'b', 'b']
    assert campaign.prepare(root, p) == m == campaign.verify(root)
    assert not (root / 'stream').exists() and bank.load(root / 'bank')[0] == {}
    assert read_json(root / 'status.json')['state'] == 'prepared_not_running'
    config['heldout_chains'] = ['a']
    atomic_json(p, config)
    with pytest.raises(ValueError, match='unique_disjoint'):
        campaign.prepare(tmp_path / 'other-run', p)
    monkeypatch.setattr(campaign, 'snapshot', lambda: {'code': 'changed'})
    with pytest.raises(RuntimeError, match='frozen_protocol_or_code_changed'):
        campaign.verify(root)


def test_native_resume_audits_artifacts_and_does_not_relaunch(protocol, monkeypatch):
    root, m = protocol
    e = {**m['stream'][0], 'task_checksum': 'pinned'}
    h = read_json(root / 'bank/HEAD.json')
    folder = root / 'native'
    p = folder / 'trials/fixed/result.json'
    atomic_json(p, {'verifier_result': {'rewards': {'reward': 0}},
                    'agent_result': {'n_input_tokens': 12, 'n_output_tokens': 5}})
    atomic_json(p.parent / 'agent/rsi_start.json', {'version': h, 'arm': 'skill-rsi', 'selected_skills': []})
    atomic_json(p.parent / 'agent/rsi_end.json', {'model_calls': 1, 'commands': 2})
    atomic_json(p.parent / 'agent/trajectory.json', {'steps': []})
    atomic_json(p.parent / 'verifier/ctrf.json', {'tests': 'private, host audit only'})
    atomic_json(folder / 'launch.json', {'started': time.time() - 10})
    monkeypatch.setattr(runtime, 'check_entry', lambda e: None)
    monkeypatch.setattr(runtime.core, 'docker_disk_status', lambda _: {'ok': True})
    monkeypatch.setattr(runtime.admission, 'compatibility', lambda *a: [])
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **kw: pytest.fail('repeated native solve'))
    result = runtime.native_solve(e, root / 'bank', h, folder, 8040, 'stream')
    assert result['reward'] == 0 and result['solve_usage']['n_input_tokens'] == 12
    assert str(p.parent / 'verifier/ctrf.json') in result['hashes']
    assert runtime.native_solve(e, root / 'bank', h, folder, 8040, 'stream') == result
    monkeypatch.setattr(runtime.admission, 'compatibility', lambda *a: ['verifier_infrastructure:missing dependency'])
    with pytest.raises(RuntimeError, match='invalid_native_result'):
        runtime.native_solve(e, root / 'bank', h, folder, 8040, 'stream')


def test_watchdog_is_diagnostic_not_resampling(protocol, monkeypatch):
    from self_skill_rsi import watchdog
    root, m = protocol
    monkeypatch.setattr(campaign, 'verify', lambda root: m)
    monkeypatch.setattr(campaign.legacy, 'alive', lambda state: False)
    value = watchdog.check(root)
    assert 'controller_not_alive' in value['alert']
    assert value['automatic_retry'] is False and value['codex_wakeup'] is False
    atomic_json(root / 'status.json', {'state': 'blocked'})
    assert watchdog.check(root)['terminal'] == 'blocked'


def test_heartbeat_is_separate_from_immutable_records(protocol):
    import threading
    root, m = protocol
    stop = threading.Event()
    thread = threading.Thread(target=campaign.heartbeat, args=(root, stop))
    thread.start()
    for _ in range(100):
        if (root / 'heartbeat.json').exists():
            break
        time.sleep(.005)
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive() and read_json(root / 'heartbeat.json')['pid']
    assert not (root / 'stream').exists()
