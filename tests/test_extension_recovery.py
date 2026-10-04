from pathlib import Path
import hashlib
import json
import pytest

from terminal_offline_expand import bootstrap as before
from terminal_offline_recover import bootstrap, grading
from terminal_offline_expand import assets
from self_skill_rsi_recover import campaign


def test_bare_pytest_retains_flags():
    script='pip install pytest==8.4.1 pytest-json-ctrf==0.3.5\npytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -v -rA\n'
    plan=bootstrap.translate(script)
    assert plan['executable']=='@pytest-shebang'
    assert '/tests/test_outputs.py -v -rA' in plan['script']
    assert plan['requirements']==['pytest==8.4.1','pytest-json-ctrf==0.3.5']


def test_break_system_packages_is_not_a_dependency():
    script='pip install pytest==8.4.1 --break-system-packages\npython -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n'
    assert bootstrap.translate(script)['requirements']==['pytest==8.4.1']


def test_existing_plan_unchanged():
    script='uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n'
    assert bootstrap.translate(script)==before.translate(script)


def test_unpinned_dependency_rejected():
    with pytest.raises(ValueError,match='literal_official_pins'):
        bootstrap.translate('pip install pytest\npytest /tests/test_outputs.py\n')


def test_old_grader_dispatch(monkeypatch,tmp_path):
    p=tmp_path/'manifest.json';p.write_text(json.dumps({'schema':'offline-safe-imports-v1'}))
    monkeypatch.setattr(grading.legacy,'load_manifest',lambda *a: {'old':True})
    assert grading.load_manifest(p,hashlib.sha256(p.read_bytes()).hexdigest())=={'old':True}


def test_nonasset_mutation_rejected(tmp_path):
    p=tmp_path/'test.py';p.write_text('assert 1\n')
    entry={'path':str(tmp_path),'task':'fixture'}
    journal={'entry':entry,'before_files':assets.files(tmp_path),'blobs':{}}
    p.write_text('assert 0\n')
    with pytest.raises(RuntimeError,match='nonasset_input'):
        assets.validate(entry,journal)


def test_recovery_keeps_order_and_official_semantics():
    assert campaign.next_entry is campaign.previous.next_entry
    assert campaign.official is campaign.previous.official
    assert campaign.report is campaign.previous.report
