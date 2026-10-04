import copy
from pathlib import Path

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_accel import campaign as c


@pytest.fixture
def prepared_source(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    m = {'schema': 'self-skill-nonregression-v2', 'model': 'Qwen/Qwen3.8-27B',
         'ports': [8040,8041,8042,8044], 'stream': [{'uid': 's0'}, {'uid': 's1'}],
         'gate': copy.deepcopy(c.v2.gate.DEFAULT), 'budgets': {'max_tokens': 32768}}
    head = {'revision': 1, 'after_task': 's0'}
    atomic_json(source / 'manifest.json', m)
    atomic_json(source / 'seal.json', {'frozen': True})
    atomic_json(source / 'bank/HEAD.json', head)
    atomic_json(source / 'bank/versions/000001/metadata.json', head)
    atomic_json(source / 'stream/s0/scored.json', {'reward': 1, 'result': '/unchanged/original/result.json'})
    atomic_json(source / 'stream/s0/source.json', {'source': 'retained'})
    atomic_json(source / 'stream/s0/update/diagnosis.json', {'diagnosis': 'retained'})
    score = source / 'stream/s0/scored.json'
    c.base.save_record(source / 'stream/s0/record.json', {'uid': 's0', 'status': 'complete',
        'before': {'revision': 0}, 'after': head, 'artifact_hashes': {str(score): c.sha(score)}})
    deployment = tmp_path / 'deployment.json'
    atomic_json(deployment, {'ports': [8040,8041], 'gpu_groups': [[0,1,2,3],[4,5,6,7]],
        'model': m['model'], 'functional_checks_passed': True, 'median_decode_speedup': 2,
        'evidence_hashes': {}})
    monkeypatch.setattr(c.offline, 'verify', lambda root: read_json(root / 'manifest.json'))
    monkeypatch.setattr(c.v2, 'check_source_stopped', lambda root: None)
    monkeypatch.setattr(c.v2, 'snapshot', lambda: {'frozen': 'v1'})
    return source, m, deployment


def test_completed_prefix_cloned_without_relabel_or_replay(prepared_source, tmp_path):
    source, original, deployment = prepared_source
    root = tmp_path / 'new'
    m = c.prepare(root, source, deployment)
    assert m['inference_migration']['preserved_official_uids'] == ['s0']
    assert m['inference_migration']['first_accelerated_solve_uid'] == 's1'
    assert read_json(root / 'stream/s0/record.json') == read_json(source / 'stream/s0/record.json')
    assert not (root / 'stream/s1').exists()
    assert c.base.learning_history(root, c.base.completed_prefix(root, m, 1))[0]['diagnosis'] == {'diagnosis': 'retained'}
    assert read_json(root / 'status.json')['new_model_calls'] == 0
    # Only HEAD is mutable; historical bank versions and task artifacts remain sealed.
    atomic_json(root / 'bank/HEAD.json', {'revision': 2})
    c.verify(root)
    atomic_json(root / 'stream/s0/scored.json', {'reward': 0})
    with pytest.raises(RuntimeError, match='recorded_evidence_changed'):
        c.verify(root)


def test_reject_pending_or_out_of_order_work(prepared_source):
    source, m, _ = prepared_source
    atomic_json(source / 'stream/s1/scored.json', {'reward': 0})
    with pytest.raises(RuntimeError, match='not_a_completed_task_boundary'):
        c.boundary(source, m)


def test_reject_bank_rewind(prepared_source):
    source, m, _ = prepared_source
    atomic_json(source / 'bank/HEAD.json', {'revision': 0})
    with pytest.raises(RuntimeError, match='skill_bank_lineage_mismatch'):
        c.boundary(source, m)


def test_reject_unproven_acceleration(prepared_source, tmp_path):
    source, m, deployment = prepared_source
    value = read_json(deployment)
    value['median_decode_speedup'] = 0.9
    atomic_json(deployment, value)
    with pytest.raises(RuntimeError, match='deployment_acceptance_missing'):
        c.prepare(tmp_path / 'new', source, deployment)
    assert not (tmp_path / 'new/stream').exists()


def test_reject_silent_algorithm_change(prepared_source, tmp_path):
    source, m, deployment = prepared_source
    root = tmp_path / 'new'
    c.prepare(root, source, deployment)
    value = read_json(root / 'manifest.json')
    value['budgets']['max_tokens'] = 123
    atomic_json(root / 'manifest.json', value)
    with pytest.raises(RuntimeError, match='changed_more_than_inference_deployment'):
        c.verify(root)


def test_same_target_is_idempotent(prepared_source, tmp_path):
    source, m, deployment = prepared_source
    root = tmp_path / 'new'
    first = c.prepare(root, source, deployment)
    assert c.prepare(root, source, deployment) == first


def test_watchdog_reports_dead_service_without_retry(tmp_path, monkeypatch):
    from self_skill_rsi_accel import watchdog as w
    deployment = tmp_path / 'deployment.json'
    atomic_json(deployment, {'services': [{'port': 8040, 'cards': '0,1,2,3', 'pid': 123, 'identity': 'x'}]})
    monkeypatch.setattr(w, 'original_check', lambda root: {'controller_alive': True})
    monkeypatch.setattr(w.campaign, 'verify', lambda root: {'inference_migration': {'deployment': str(deployment)}})
    monkeypatch.setattr(w.campaign, 'report', lambda root, m: {'completed': 6})
    monkeypatch.setattr(w.campaign.base.legacy, 'alive', lambda service: False)
    result = w.check(tmp_path)
    assert result['alert'] == 'inference_service_unhealthy:8040; no_trial_retry'
    assert result['inference_services'][8040]['healthy'] is False


def test_watchdog_retains_original_alert(tmp_path, monkeypatch):
    from self_skill_rsi_accel import watchdog as w
    deployment = tmp_path / 'deployment.json'
    atomic_json(deployment, {'services': [{'port': 8040, 'cards': '0,1,2,3', 'pid': 123, 'identity': 'x'}]})
    monkeypatch.setattr(w, 'original_check', lambda root: {'controller_alive': True, 'alert': 'controller_heartbeat_stale'})
    monkeypatch.setattr(w.campaign, 'verify', lambda root: {'inference_migration': {'deployment': str(deployment)}})
    monkeypatch.setattr(w.campaign, 'report', lambda root, m: {})
    monkeypatch.setattr(w.campaign.base.legacy, 'alive', lambda service: False)
    assert w.check(tmp_path)['alert'] == 'controller_heartbeat_stale'


def test_launcher_restores_registered_docker_environment(monkeypatch):
    from self_skill_rsi_accel import environment as e
    monkeypatch.setattr(Path, 'is_file', lambda self: True)
    monkeypatch.setattr(Path, 'is_socket', lambda self: True)
    monkeypatch.setattr(e.base.core, 'runner_environment', lambda port: {'PATH': '/usr/bin'})
    monkeypatch.setattr(e.subprocess, 'check_output', lambda *a, **kw:
                        '{"Driver":"overlay2","DockerRootDir":"/data2/ytzhang/rsi-harness-docker-overlay2"}')
    env = e.runner_environment(8040)
    assert env['DOCKER_HOST'] == 'unix:///tmp/rsi-harness-docker-overlay2/docker.sock'
    assert env['PATH'].split(':')[0] == '/home/ytzhang/budget-rl/.deps/docker-static'


def test_launcher_rejects_wrong_daemon(monkeypatch):
    from self_skill_rsi_accel import environment as e
    monkeypatch.setattr(Path, 'is_file', lambda self: True)
    monkeypatch.setattr(Path, 'is_socket', lambda self: True)
    monkeypatch.setattr(e.base.core, 'runner_environment', lambda port: {'PATH': '/usr/bin'})
    monkeypatch.setattr(e.subprocess, 'check_output', lambda *a, **kw:
                        '{"Driver":"vfs","DockerRootDir":"/wrong"}')
    with pytest.raises(RuntimeError, match='wrong_docker_daemon'):
        e.runner_environment(8040)
