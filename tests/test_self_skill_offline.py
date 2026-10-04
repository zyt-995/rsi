from pathlib import Path

import pytest

from self_skill_rsi_offline import campaign


def test_adapter_changes_only_verifier_and_restores_functions(monkeypatch):
    m = {'offline_migration': {'manifest': '/local/manifest.json', 'manifest_sha256': 'hash'}}
    monkeypatch.setattr(campaign, 'verify', lambda root: m)
    original = campaign.base.core.harbor_command
    previous_verify = campaign.v2.verify
    monkeypatch.setattr(campaign, 'ORIGINAL_COMMAND', lambda *a, **kw: ['harbor', 'trials', 'start', '--model', 'unchanged'])
    with campaign.adapter(Path('/unused')):
        command = campaign.base.core.harbor_command()
        assert command[:5] == ['harbor', 'trials', 'start', '--model', 'unchanged']
        assert command[5:] == ['--verifier', 'offline_verifier.harbor:OfflineVerifier',
            '--verifier-kwarg', 'manifest=/local/manifest.json', '--verifier-kwarg', 'manifest_sha256=hash']
    assert campaign.base.core.harbor_command is original
    assert campaign.v2.verify is previous_verify


def test_adapter_restores_on_exception(monkeypatch):
    monkeypatch.setattr(campaign, 'verify', lambda root: {'offline_migration': {'manifest': 'm', 'manifest_sha256': 'h'}})
    original = campaign.base.core.harbor_command
    with pytest.raises(RuntimeError):
        with campaign.adapter(Path('/unused')):
            raise RuntimeError('trial failed')
    assert campaign.base.core.harbor_command is original


def test_no_in_place_handoff(tmp_path):
    with pytest.raises(ValueError, match='separate'):
        campaign.prepare(tmp_path, tmp_path, None, None)


def test_live_source_not_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign.base.legacy, 'alive', lambda _: True)
    with pytest.raises(RuntimeError, match='live'):
        campaign.stopped(tmp_path)
