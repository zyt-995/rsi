"""Reject dependency and source drift before a model can be called."""
import hashlib
import json
import platform
from pathlib import Path
from rsi_portable import reproducibility as module


def fake_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'REPO', tmp_path)
    (tmp_path / 'upstream').mkdir()
    (tmp_path / 'upstream/manifest.json').write_text('{"repositories": []}')
    (tmp_path / 'requirements.lock').write_text('pytest==9.1.1\n')
    (tmp_path / 'solver.py').write_text('frozen solver\n')
    value = {'id': 'test-reference', 'python': platform.python_version(),
             'files': {'solver.py': hashlib.sha256(b'frozen solver\n').hexdigest()},
             'upstream_diff_sha256': {}}
    (tmp_path / 'upstream/reproduction-reference.json').write_text(json.dumps(value))


def test_reference_accepts_matching_software_without_provider_calls(tmp_path, monkeypatch):
    fake_reference(tmp_path, monkeypatch)
    result = module.verify(software_only=True)
    assert result['passed'] and result['paid_model_calls'] == 0


def test_same_package_name_with_different_version_is_rejected(tmp_path, monkeypatch):
    fake_reference(tmp_path, monkeypatch)
    monkeypatch.setattr(module.importlib.metadata, 'version', lambda name: '9.1.0')
    result = module.verify(software_only=True)
    assert not result['passed'] and any('pytest' in e for e in result['errors'])


def test_modified_solver_cannot_be_declared_equivalent(tmp_path, monkeypatch):
    fake_reference(tmp_path, monkeypatch)
    (tmp_path / 'solver.py').write_text('different solver\n')
    result = module.verify(software_only=True)
    assert not result['passed'] and any('solver.py' in e for e in result['errors'])
