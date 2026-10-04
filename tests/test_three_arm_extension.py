import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from terminal_offline_expand.bootstrap import translate
from self_skill_rsi_expand import campaign, scheduler, worker
from rsi_harness.clean_campaign import DATASET


@pytest.mark.parametrize('task,minor', [('adaptive-rejection-sampler','3.13'), ('adaptive-rejection-sampler-EVO-1','3.12')])
def test_official_python_minor_and_pins_are_preserved(task, minor):
    p = translate((DATASET / task / 'tests/test.sh').read_text())
    assert p['python_minor'] == minor
    assert ('numpy==2.2.6' if minor == '3.12' else 'numpy==2.3.3') in p['requirements']
    assert '/tests/test_outputs.py' in p['script']


def test_absolute_native_python_is_preserved():
    p = translate((DATASET / 'financial-document-processor-EVO-1/tests/test.sh').read_text())
    assert p['executable'] == '/opt/fdp/bin/python'
    assert p['runtime'] == 'native'


def test_unpinned_or_executable_requirements_fail_closed():
    with pytest.raises(ValueError, match='literal_official_pins'):
        translate('pip install pytest\npython -m pytest /tests/test_outputs.py')


def test_ready_chain_lagging_arm_selected_first(tmp_path, monkeypatch):
    entries = {'baseline': {'uid':'s000115', 'chain':'c'}, 'skill-rsi': {'uid':'s000113','chain':'c'},
               'self-skill': {'uid':'s000112','chain':'c'}}
    monkeypatch.setattr(campaign, 'next_entry', lambda r,m,a: entries[a])
    monkeypatch.setattr(campaign, 'acceptance', lambda r,c: {'passed':True})
    assert [a for e,a in scheduler.candidates(tmp_path,{},set())] == ['self-skill','skill-rsi','baseline']
    assert 'self-skill' not in [a for e,a in scheduler.candidates(tmp_path,{}, {'self-skill'})]


def test_unaccepted_chain_never_scheduled(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign,'next_entry',lambda *a:{'uid':'s000112','chain':'bad'})
    monkeypatch.setattr(campaign,'acceptance',lambda *a:None)
    assert scheduler.candidates(tmp_path,{},set()) == []


def test_dead_unfinished_job_is_review_not_retry(tmp_path, monkeypatch):
    p=tmp_path/'jobs/self-skill/s000112/launch.json';p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'pid':123,'arm':'self-skill','uid':'s000112'}))
    monkeypatch.setattr(campaign.base.legacy,'alive',lambda _:False)
    active, errors=scheduler.jobs(tmp_path)
    assert not active and 'no_automatic_replay' in errors[0]['reason']


def test_completed_job_is_not_replayed(tmp_path,monkeypatch):
    folder=tmp_path/'jobs/baseline/s000112';folder.mkdir(parents=True)
    (folder/'launch.json').write_text(json.dumps({'pid':123,'arm':'baseline','uid':'s000112'}))
    (folder/'result.json').write_text('{"state":"complete"}')
    monkeypatch.setattr(campaign.base.legacy,'alive',lambda _:False)
    assert scheduler.jobs(tmp_path)==([],[])


def test_launch_intent_blocks_duplicate_submission(tmp_path):
    folder=tmp_path/'jobs/baseline/s000112';folder.mkdir(parents=True)
    (folder/'intent.json').write_text('{}')
    with pytest.raises(RuntimeError,match='interrupted_job_launch'):
        scheduler.launch(tmp_path,{'uid':'s000112'},'baseline',8041,{})


def test_new_control_requires_network_admission(tmp_path):
    e={'chain':'c','task':'t','uid':'s000112'}
    p=campaign.control_record(tmp_path,e,'baseline');p.parent.mkdir(parents=True)
    p.write_text('{"status":"complete","reward":1}')
    m={'extension':{'reused_controls':{'baseline':[]}}}
    assert campaign.official(tmp_path,e,'baseline',m) is None
    (p.parent/'network-admission.json').write_text('{"accepted":true}')
    assert campaign.official(tmp_path,e,'baseline',m)['reward']==1


def test_command_adapter_preserves_solver_budget_and_routes_offline(tmp_path,monkeypatch):
    original=worker.clean.harbor_command
    monkeypatch.setattr(worker,'grader',lambda *a:(Path('/data2/grader.json'),'digest',{}))
    with worker.adapter(tmp_path,{}):
        args=worker.clean.harbor_command(DATASET/'cobol-modernization-EVO-1',tmp_path/'trials','baseline',tmp_path/'state')
        assert '--verifier' in args and 'manifest=/data2/grader.json' in args
        assert 'max_turns=15' in args
        assert any('17898' in s for s in args)
        assert not any('17897' in s for s in args)
    assert worker.clean.harbor_command is original


def test_registered_queue_is_full_and_nonduplicated():
    p=Path('/data2/ytzhang/rsi-harness-runs/three-arm-extension-20261004-v1/queue.json')
    if not p.exists():pytest.skip('deployment not available')
    q=json.loads(p.read_text());entries=q['prior']+q['remaining']
    assert len(entries)==441 and len({e['task'] for e in entries})==441
    assert len(q['remaining'])==329 and q['remaining'][0]['task']=='cobol-modernization-EVO-1'
    assert [e['uid'] for e in entries]==[f's{i:06d}' for i in range(441)]
