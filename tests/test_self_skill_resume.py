from pathlib import Path
import json
import pytest
from self_skill_rsi_resume.bootstrap import translate


def test_all_remaining_bootstraps_preserve_official_test_work():
    root=Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261001-dual-v1')
    if not root.exists():pytest.skip('deployment-specific inventory')
    m=json.loads((root/'manifest.json').read_text())
    for e in m['stream'][24:]:
        original=(Path(e['path'])/'tests/test.sh').read_text()
        plan=translate(original)
        for command in ('cp /tests/test.py /app/test.py','cd /tmp || exit 1',
                        'make clean && ./configure && make -j2',
                        'make clean && ./configure && make -j4',
                        'make -C testsuite one DIR=tests/basic',
                        'ORIGINAL_EXIT_CODE=$?', 'ADDITIONAL_EXIT_CODE=$?'):
            if command in original:assert command in plan['script']
        assert '/tests/test_outputs.py' in plan['script']
        assert 'apt-get update' not in plan['script']
        assert 'https://astral.sh/' not in plan['script']
        assert plan['pytest_calls']==(2 if e['chain']=='fix-code-vulnerability' else 1)


def test_unknown_dependency_is_not_silently_stripped():
    with pytest.raises(ValueError,match='system_dependencies'):
        translate('apt-get install -y malicious-package\npython -m pytest /tests/test_outputs.py')


def test_missing_test_command_is_not_success():
    with pytest.raises(ValueError):translate('echo 1 > /logs/verifier/reward.txt')


def test_native_python_and_working_directory_preserved():
    p=translate('cd /tmp || exit 1\npython3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA')
    assert p['runtime']=='native' and p['executable']=='python3'
    assert p['script'].startswith('cd /tmp || exit 1\n')
    assert p['requirements']==[]


def test_migration_changes_only_infrastructure_references(tmp_path):
    import copy
    from self_skill_rsi_resume.campaign import changed_manifest
    grader=tmp_path/'grader.json';grader.write_text('{}')
    old={'model':'same','solve':{'steps':15},'memory':{'cross_chain':True},
         'learning':{'budget':42},'gate':{'tie':'accept'},'stream':[{'task':'old'},{'task':'next'}],
         'inference_migration':{'deployment':'before'},
         'resilience_migration':{'grader':'old','grader_sha256':'old'},
         'continuation':{'ready_grader_tasks':['old'],'candidate_validation_parallelism':2}}
    saved=copy.deepcopy(old)
    new=changed_manifest(old,tmp_path/'deployment.json',grader,['old','next'])
    assert old==saved
    for key in ('model','solve','memory','learning','gate','stream'):assert new[key]==old[key]
    assert new['continuation']['candidate_validation_parallelism']==2


def test_two_repository_suites_and_original_reward_condition_preserved():
    raw='''python -m pytest --ctrf /logs/verifier/original-repo-ctrf.json -rA
ORIGINAL_EXIT_CODE=$?
python -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
ADDITIONAL_EXIT_CODE=$?
if [ $ORIGINAL_EXIT_CODE -eq 0 ] && [ $ADDITIONAL_EXIT_CODE -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
'''
    plan=translate(raw)
    assert plan['pytest_calls']==2
    assert 'original-repo-ctrf.json' in plan['script']
    assert 'if [ $ORIGINAL_EXIT_CODE -eq 0 ] && [ $ADDITIONAL_EXIT_CODE -eq 0 ]; then' in plan['script']


def test_ocaml_tag_and_rebuild_are_never_removed():
    from self_skill_rsi_resume.bootstrap import OCAML_URL,OCAML_TAG
    text=f'''git clone {OCAML_URL} ocaml-original && cd ocaml-original && git checkout {OCAML_TAG} && cd ..
rm -rf ocaml/testsuite && cp -r ocaml-original/testsuite ocaml/
cd ocaml
make clean && ./configure && make -j2
rm tests.txt
make -C testsuite one DIR=tests/basic | tee tests.txt
python3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
'''
    plan=translate(text)
    assert plan['ocaml_fixture'] is True
    assert text.split('python3 -m pytest')[0] in plan['script']


def test_security_archive_without_legacy_checksum_is_not_dropped(tmp_path,monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from self_skill_rsi_resume import offline
    data=b'deb';digest=hashlib.sha256(data).hexdigest()
    def docker(args,**kw):
        if 'apt-cache' in args:
            return SimpleNamespace(stdout=f'Package: pkg\nVersion: 1.0\nFilename: pool/pkg_1.0_amd64.deb\nSize: 3\nSHA256: {digest}\n')
        return SimpleNamespace(stdout="'http://deb.debian.org/debian-security/pool/pkg_1.0_amd64.deb' pkg_1.0_amd64.deb 3 \n",stderr='')
    def curl(args,**kw):
        Path(args[args.index('--output')+1]).write_bytes(data)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(offline.runner,'docker',docker)
    monkeypatch.setattr(offline.subprocess,'run',curl)
    offline.cache_apt_packages('fake-container',['pkg'],tmp_path)
    value=json.loads((tmp_path/'archive-provenance.json').read_text())
    assert value['all_uris_accounted'] and len(value['archives'])==1
    assert value['archives'][0]['algorithm']=='sha256'
    assert (tmp_path/'pkg_1.0_amd64.deb').read_bytes()==data
