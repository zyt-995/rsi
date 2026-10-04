import json
from pathlib import Path
import runpy

M = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/review-active-self-skill.py'))


def test_read_only_by_default():
    args=M['codex_command'](Path('/tmp/summary'))
    assert args[args.index('--sandbox')+1]=='read-only'


def test_repair_is_scoped_and_noninteractive():
    args=M['codex_command'](Path('/tmp/summary'),True)
    assert args[args.index('--sandbox')+1]=='workspace-write'
    assert 'danger-full-access' not in args
    assert args[args.index('--ask-for-approval')+1]=='never'
    assert 'sandbox_workspace_write.network_access=true' in args


def test_repair_requires_explicit_run_authority(tmp_path):
    assert not M['repair_needed']({'status':{'state':'awaiting_environment'}},tmp_path)
    (tmp_path/'recovery').mkdir()
    (tmp_path/'recovery/autorepair-policy.json').write_text(json.dumps({'enabled':True,'root':str(tmp_path)}))
    assert M['repair_needed']({'status':{'state':'awaiting_environment'}},tmp_path)
    assert M['repair_needed']({'status':None},tmp_path)
    assert not M['repair_needed']({'status':{'state':'complete'}},tmp_path)


def test_foreground_lease_prevents_a_second_writer(tmp_path):
    (tmp_path/'recovery').mkdir()
    (tmp_path/'recovery/autorepair-policy.json').write_text(json.dumps({'enabled':True,'root':str(tmp_path)}))
    (tmp_path/'recovery/manual-repair.json').write_text(json.dumps({'until':1e20}))
    assert not M['repair_needed']({'status':{'state':'needs_attention'}},tmp_path)
