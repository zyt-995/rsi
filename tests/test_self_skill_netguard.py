import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from self_skill_rsi_netguard import health, runtime, campaign


def round_at(t, codes=(200, 200, 200)):
    return {'time': t, 'checks': [{'url': 'https://' + name, 'http_status': str(code),
        'exit_code': 0 if code else 28, 'ok': 200 <= code < 400}
        for name, code in zip(('github.com', 'compcert.org', 'mirror.example'), codes)]}


def trace(tmp_path, content='HTTP/2 200', message=''):
    p = tmp_path / 'agent/trajectory.json'; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'steps': [{'message': message, 'timestamp': 'x',
        'observation': {'results': [{'content': content}]}}]}))
    return p


@pytest.mark.parametrize('code', [403, 404, 429, 503, 0])
def test_one_origin_warning_does_not_invalidate_task(tmp_path, code):
    value = round_at(0, (code, 200, 200))
    assert health.classify_round(value)['ready']
    assert not health.classify_round(value)['transport_outage']
    assert health.decide(trace(tmp_path), [value])['accepted']


def test_sustained_multi_origin_failure_is_rejected(tmp_path):
    rounds = [round_at(t, (0, 0, 200)) for t in (0, 60, 120)]
    result = health.decide(trace(tmp_path), rounds)
    assert not result['accepted'] and result['reason'] == 'sustained_multi_origin_outage'


def test_healthy_round_resets_outage_streak():
    assert not health.sustained_outage([round_at(0, (0, 0, 0)), round_at(60),
        round_at(120, (0, 0, 0)), round_at(180, (0, 0, 0))])


def test_three_immediate_preflight_probes_not_sustained_outage():
    assert not health.sustained_outage([round_at(t, (0, 0, 0)) for t in (0, 10, 20)])


def test_http_407_is_proxy_failure():
    assert health.classify_round(round_at(0, (407, 407, 407)))['transport_outage']


def test_model_claim_is_not_transport_evidence(tmp_path):
    p = trace(tmp_path, message='curl: (7) Failed to connect to 10.0.2.2 port 17898')
    assert health.decide(p, [round_at(0)])['accepted']


@pytest.mark.parametrize('text', [
    'curl: (7) Failed to connect to 10.0.2.2 port 17898 after 0 ms: Could not connect to server',
    'W: Failed to fetch https://example.org/file Could not connect to example.org',
    "fatal: unable to access 'https://example.org': Could not resolve host: example.org",
    'curl: (28) Operation timed out after 20000 milliseconds'])
def test_actual_task_transport_failure_not_hidden_by_healthy_probes(tmp_path, text):
    assert not health.decide(trace(tmp_path, text), [round_at(0)])['accepted']


def test_wrong_repository_and_missing_executable_are_algorithmic(tmp_path):
    p = trace(tmp_path, 'HTTP/2 404\n  "message": "Not Found",\ntimeout: failed to run command curl: No such file or directory\nHTTP/2 200')
    assert health.decide(p, [round_at(0, (403, 200, 200))])['accepted']


def test_monitor_files_never_overwrite_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.time, 'time_ns', lambda: 123)
    runtime.save_probe(tmp_path, 'during', round_at(1))
    runtime.save_probe(tmp_path, 'during', round_at(2))
    assert len(runtime.all_probes(tmp_path)) == 2


def test_old_failed_rounds_survive_restart(tmp_path):
    for t in (0, 60, 120): runtime.save_probe(tmp_path, 'during', round_at(t, (0, 0, 0)))
    runtime.save_probe(tmp_path, 'postflight', round_at(180))
    p = trace(tmp_path / 'raw')
    record = {'result': str(p.parent.parent / 'result.json'), 'reward': 0}
    with pytest.raises(RuntimeError, match='requires_review'):
        runtime.admit(tmp_path, record)
    assert not (tmp_path / 'record.json').exists()


def test_original_zero_is_preserved(tmp_path):
    runtime.save_probe(tmp_path, 'preflight', round_at(1, (403, 200, 200)))
    p = trace(tmp_path / 'raw')
    record = {'result': str(p.parent.parent / 'result.json'), 'reward': 0}
    assert runtime.admit(tmp_path, record)['reward'] == 0
    assert json.loads((tmp_path / 'network-admission.json').read_text())['reward_used_for_decision'] is False


def test_preflight_failure_never_starts_model(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'SOURCE', tmp_path / 'previous')
    monkeypatch.setattr(runtime.old, 'SOURCE', tmp_path / 'older')
    monkeypatch.setattr(runtime.time, 'sleep', lambda _: None)
    monkeypatch.setattr(health, 'sample', lambda: {**round_at(0, (0, 0, 0)), 'assessment': {'ready': False}})
    def no_model(*a, **k): raise AssertionError('unexpected model call')
    monkeypatch.setattr(runtime.old, 'ORIGINAL_NATIVE', no_model)
    with pytest.raises(RuntimeError, match='no_model_call'):
        runtime.native(tmp_path, {'uid': 's1', 'task': 't'}, None, {}, tmp_path / 'stream/s1/solve', 1, 'stream', manifest={})


def test_persisted_monitor_error_blocks_restart(tmp_path):
    folder = tmp_path / 'stream/s1/solve'; folder.mkdir(parents=True)
    (folder / 'network-monitor-error.json').write_text('{}')
    with pytest.raises(RuntimeError, match='persisted_network_monitor_error'):
        runtime.native(tmp_path, {}, None, {}, folder, 1, 'stream', manifest={})


def test_migration_changes_only_deployment_reference():
    original = {'inference_migration': {'deployment': 'old'}, 'model': 'same', 'stream': [1, 2],
                'solve': {'steps': 15}, 'learning': {'rules': 'same'}, 'gate': {'ties': True}}
    expected = campaign.expected_manifest(original, Path('/new'))
    assert original['inference_migration']['deployment'] == 'old'
    for k in original:
        if k != 'inference_migration': assert expected[k] == original[k]


def test_real_quarantined_task_is_admissible_without_rerun():
    root = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261002-network-v1/stream/s000042/solve')
    if not root.exists(): pytest.skip('deployment evidence unavailable')
    record = json.loads((root / 'guarded-execution/record.json').read_text())
    result = health.decide(Path(record['result']).parent / 'agent/trajectory.json',
                          [json.loads(p.read_text()) for p in (root / 'network').glob('*.json')])
    assert result['accepted'] and result['warning_rounds'] == 2
    assert result['positive_network_evidence'] and not result['transport_failures']


def test_restart_admits_finished_native_without_calling_model(tmp_path, monkeypatch):
    folder = tmp_path / 'stream/s1/solve'; (folder / 'execution').mkdir(parents=True)
    p = trace(tmp_path / 'raw')
    record = {'result': str(p.parent.parent / 'result.json'), 'reward': 0}
    (folder / 'execution/record.json').write_text(json.dumps(record))
    runtime.save_probe(folder, 'preflight', round_at(0))
    monkeypatch.setattr(runtime.old.v2.gate, 'validate_record', lambda *a: None)
    monkeypatch.setattr(health, 'sample', lambda: round_at(60))
    def no_model(*a, **kw): raise AssertionError('completed solve was repeated')
    monkeypatch.setattr(runtime.old, 'ORIGINAL_NATIVE', no_model)
    assert runtime.native(tmp_path, {}, None, {}, folder, 1, 'stream', manifest={}) == record
    assert len(list((folder / 'network-v2').glob('*-postflight-*.json'))) == 1


def test_restart_does_not_erase_prior_outage(tmp_path, monkeypatch):
    folder = tmp_path / 'stream/s1/solve'; (folder / 'execution').mkdir(parents=True)
    p = trace(tmp_path / 'raw')
    (folder / 'execution/record.json').write_text(json.dumps({'result': str(p.parent.parent / 'result.json'), 'reward': 1}))
    for t in (0, 60, 120): runtime.save_probe(folder, 'during', round_at(t, (0, 0, 0)))
    monkeypatch.setattr(runtime.old.v2.gate, 'validate_record', lambda *a: None)
    monkeypatch.setattr(health, 'sample', lambda: round_at(180))
    with pytest.raises(RuntimeError, match='requires_review'):
        runtime.native(tmp_path, {}, None, {}, folder, 1, 'stream', manifest={})
    assert not (folder / 'record.json').exists()
