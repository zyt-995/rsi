import json
from pathlib import Path
from contextlib import contextmanager

import pytest
from self_skill_rsi_network import audit, campaign as c


def test_audit_uses_observation_not_model_claims(tmp_path):
    p = tmp_path / 'trajectory.json'
    p.write_text(json.dumps({'steps': [{'message': '10.0.2.2:17897 Connection refused',
        'observation': {'results': [{'content': 'all good'}]}}]}))
    assert audit.proxy_failures(p) == []
    p.write_text(json.dumps({'steps': [{'timestamp': 'observed-time', 'observation': {'results': [
        {'content': 'Connecting to 10.0.2.2:17897... failed: Connection refused.'}]}}]}))
    assert audit.proxy_failures(p)[0]['time'] == 'observed-time'


def test_audit_new_proxy_and_separate_port_syntax(tmp_path):
    p = tmp_path / 'trajectory.json'
    p.write_text(json.dumps({'steps': [{'observation': {'results': [{'content':
        'curl: (7) Failed to connect to 10.0.2.2 port 17898 after 0 ms: Could not connect to server'}]}}]}))
    assert len(audit.proxy_failures(p)) == 1


def test_reuse_requires_identical_head_and_no_proxy_failure(monkeypatch):
    entry = {'uid': 's000040', 'task': 'task'}
    record = {**entry, 'before': {'revision': 2}, 'purpose': 'stream'}
    monkeypatch.setattr(c.audit, 'record_failures', lambda r: [])
    assert c.can_reuse(record, entry, {'revision': 2}, 'stream')
    assert not c.can_reuse(record, entry, {'revision': 3}, 'stream')
    monkeypatch.setattr(c.audit, 'record_failures', lambda r: ['connection refused'])
    assert not c.can_reuse(record, entry, {'revision': 2}, 'stream')


def test_unhealthy_preflight_never_calls_model(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'SOURCE', tmp_path / 'old')
    monkeypatch.setattr(c.proxy, 'health', lambda: {'passed': False})
    def forbidden(*a, **kw): raise AssertionError('model called')
    monkeypatch.setattr(c, 'ORIGINAL_NATIVE', forbidden)
    with pytest.raises(RuntimeError, match='preflight_failed_no_model_call'):
        c.guarded_native(tmp_path, {'uid': 's1', 'task': 'x'}, None, {}, tmp_path / 'stream/s1/solve', 1, 'stream', manifest={})


def test_postflight_outage_does_not_publish_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'SOURCE', tmp_path / 'old')
    checks = iter([{'passed': True}, {'passed': False}])
    monkeypatch.setattr(c.proxy, 'health', lambda: next(checks))
    monkeypatch.setattr(c.audit, 'record_failures', lambda r: [])
    monkeypatch.setattr(c, 'ORIGINAL_NATIVE', lambda *a, **kw: {'reward': 0, 'result': 'raw-result.json'})
    folder = tmp_path / 'stream/s1/solve'
    with pytest.raises(RuntimeError, match='not_admitted_as_score'):
        c.guarded_native(tmp_path, {'uid': 's1', 'task': 'x'}, None, {}, folder, 1, 'stream', manifest={})
    assert not (folder / 'record.json').exists()
    assert json.loads((folder / 'network-rejected.json').read_text())['score'] is None


def test_good_network_retains_algorithmic_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'SOURCE', tmp_path / 'old')
    monkeypatch.setattr(c.proxy, 'health', lambda: {'passed': True})
    monkeypatch.setattr(c.audit, 'record_failures', lambda r: [])
    monkeypatch.setattr(c, 'ORIGINAL_NATIVE', lambda *a, **kw: {'reward': 0, 'result': 'raw-result.json'})
    folder = tmp_path / 'stream/s1/solve'
    value = c.guarded_native(tmp_path, {'uid': 's1', 'task': 'x'}, None, {}, folder, 1, 'stream', manifest={})
    assert value['reward'] == 0 and (folder / 'network-accepted.json').exists()


def test_adapter_changes_only_proxy(monkeypatch):
    original = ['harbor', '--ae', 'HTTP_PROXY=http://10.0.2.2:17897', '--agent-kwarg', 'max_turns=15',
                '--ve', 'https_proxy=http://10.0.2.2:17897', '--model', 'same-model']
    @contextmanager
    def adapter(m): yield
    monkeypatch.setattr(c, 'ORIGINAL_ADAPTER', adapter)
    monkeypatch.setattr(c.base.core, 'harbor_command', lambda *a: original[:])
    with c.adapter({}):
        new = c.base.core.harbor_command()
    assert new == [v.replace('17897', '17898') for v in original]
    assert c.base.core.harbor_command() == original


def test_changed_history_does_not_reuse_old_proposal(tmp_path, monkeypatch):
    source = tmp_path / 'source'; root = tmp_path / 'new'; rel = Path('stream/s1/update')
    old_folder = source / rel; old_folder.mkdir(parents=True)
    (old_folder / 'input.json').write_text(json.dumps({'history': ['old-history']}))
    (old_folder / 'result.json').write_text('{}')
    monkeypatch.setattr(c, 'SOURCE', source)
    monkeypatch.setattr(c, 'ORIGINAL_PROPOSE', lambda *a: 'fresh')
    assert c.reuse_proposal(root, root / rel, {}, {}, {}, ['changed-history'], 'm', 'url') == 'fresh'
    assert not (root / rel).exists()
