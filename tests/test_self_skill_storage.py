from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from self_skill_rsi_storage import campaign, storage, services


def test_only_storage_and_service_concurrency_change():
    old = {'ports': [8041, 8040], 'inference_migration': {'deployment': 'old'},
           'model': 'same', 'stream': [1, 2], 'gate': {'max_cases': 2}, 'solve': {'budget': 15}}
    new = campaign.expected_manifest(old, Path('/new'))
    assert old['ports'] == [8041, 8040]
    assert new['ports'] == [8041]
    for k in ('model', 'stream', 'gate', 'solve'):
        assert new[k] == old[k]


def test_runtime_environment_on_data2(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'ROOT', tmp_path)
    env = storage.environment()
    for k in ('TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME', 'UV_CACHE_DIR', 'PIP_CACHE_DIR'):
        assert Path(env[k]).is_relative_to(tmp_path)
        assert Path(env[k]).is_dir()


def make_boundary(tmp_path):
    for uid in ('s000093', 's000092'):
        p = tmp_path / 'validation' / uid / '00/candidate/execution/failure.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'reason': 'disk_admission_failed:details'}))
    return tmp_path


def test_prelaunch_failure_is_not_a_model_replay(tmp_path):
    assert len(campaign.audit_unstarted_validation(make_boundary(tmp_path))) == 2


@pytest.mark.parametrize('name', ['launch.json', 'child.json', 'result.json'])
def test_refuses_to_rerun_started_validation(tmp_path, name):
    stage = make_boundary(tmp_path)
    (stage / 'validation' / name).write_text('{}')
    with pytest.raises(RuntimeError, match='already_started'):
        campaign.audit_unstarted_validation(stage)


def test_refuses_to_discard_unrelated_failure(tmp_path):
    stage = make_boundary(tmp_path)
    (stage / 'validation/s000093/00/candidate/execution/failure.json').write_text('{"reason":"network"}')
    with pytest.raises(RuntimeError, match='expected_two'):
        campaign.audit_unstarted_validation(stage)


def test_wrong_docker_disk_fails_closed(monkeypatch):
    monkeypatch.setattr(storage.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='/var/lib/docker'))
    assert not storage.disk_status(Path('/data2'))['ok']


@pytest.mark.parametrize('host, data, accepted', [(3, 1000, True), (0.5, 1000, False), (3, 99, False)])
def test_actual_storage_admission(monkeypatch, host, data, accepted):
    monkeypatch.setattr(storage.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(storage.DOCKER)))
    monkeypatch.setattr(storage.Path, 'resolve', lambda p: p)
    monkeypatch.setattr(storage.Path, 'stat', lambda p: SimpleNamespace(st_dev=3))
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda p: SimpleNamespace(free=(host if str(p) == '/' else data) * 1024**3))
    assert storage.disk_status(Path('/data2/run'))['ok'] == accepted


def test_runtime_not_on_data2_is_rejected(monkeypatch):
    monkeypatch.setattr(storage.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(storage.DOCKER)))
    monkeypatch.setattr(storage.Path, 'stat', lambda p: SimpleNamespace(st_dev=3))
    assert not storage.disk_status(Path('/tmp'))['ok']


def test_exactly_one_tp4_group_and_original_primary_port():
    assert services.PORTS == [8041]
    assert services.GROUPS == [[5, 6, 7, 8]]


def test_busy_cards_never_reclaimed(monkeypatch):
    monkeypatch.setattr(services.subprocess, 'check_output', lambda *a, **k: '\n'.join(f'{i},uuid{i},1000' for i in range(10)))
    with pytest.raises(RuntimeError, match='no_longer_free'):
        services.free_cards()
