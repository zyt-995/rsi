from contextlib import nullcontext
from pathlib import Path

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_continue import campaign as c, offline
from test_self_skill_rsi_v2 import Native, protocol, proposer


def test_match_selection_preserves_order_and_zeros_and_excludes_seen(tmp_path, monkeypatch):
    atomic_json(tmp_path/'scheduler_manifest.json', {'chain_order':['b','a'],
        'task_order':{'a':['a','a-EVO-1'],'b':['b']}})
    for task,chain in [('b','b'),('a','a'),('a-EVO-1','a')]:
        for arm in ('baseline','skill-rsi'):
            folder = tmp_path/chain/arm/'stages'/task
            atomic_json(folder/'result.json', {'reward':0})
            atomic_json(folder/'record.json', {'status':'complete','reward':0,
                        'result':str(folder/'result.json')})
    monkeypatch.setattr(c.base.core,'baseline_compatibility',lambda path,task: [])
    m = c.matched_tasks(tmp_path, {'b'})
    assert [r['task'] for r in m['common']] == ['b','a','a-EVO-1']
    assert [r['task'] for r in m['additional']] == ['a','a-EVO-1']
    assert all(r['controls']['baseline']['reward'] == 0 for r in m['common'])
    monkeypatch.setattr(c.base.core,'baseline_compatibility',lambda path,task: ['model'] if task=='a' else [])
    m = c.matched_tasks(tmp_path, {'b'})
    assert [r['task'] for r in m['additional']] == []  # Never skip a chain predecessor.
    assert m['excluded'][0]['task'] == 'a'


def test_next_bootstrap_matches_exact_existing_plain_bootstrap():
    for name in c.base.core.tasks('build-pmars'):
        assert offline.bootstrap_profile(c.base.core.DATASET/name) == 'plain'


def test_changed_bootstrap_is_not_silently_accepted(tmp_path):
    task = tmp_path/'build-pmars'
    (task/'tests').mkdir(parents=True)
    (task/'tests/test.sh').write_text('exit 0\n')
    with pytest.raises(RuntimeError,match='bootstrap_differs'):
        offline.bootstrap_profile(task)


def test_one_port_uses_same_step_gate_and_cross_chain_memory(protocol):
    root,m = protocol
    m['ports'] = [8041]
    native = Native(score=0, validation_score=0)
    records = [c.v2.step(root,m,i,solve=native,proposer=proposer,checker=lambda _:None)
               for i in range(len(m['stream']))]
    assert records[1]['before'] == records[0]['after']
    assert records[2]['before'] == records[1]['after']
    assert records[0]['promotion_reason'] == 'non_regression_tie'
    count = len(native.calls)
    c.v2.step(root,m,1,solve=native,proposer=proposer,checker=lambda _:None)
    assert len(native.calls) == count
    assert sum(purpose=='stream' for _,purpose,_,_ in native.calls) == 3
    assert not any('/parent/' in folder for *_,folder in native.calls)


def test_running_continuation_skips_complete_prefix_and_waits_before_unsupported_task(tmp_path, monkeypatch):
    root = tmp_path
    entries = [{'uid':'s0','task':'old'},{'uid':'s1','task':'next'}]
    m = {'stream':entries,'ports':[8041], 'resilience_migration':{'grader':'x','grader_sha256':'y'}}
    atomic_json(root/'stream/s0/record.json', {'complete':True})
    monkeypatch.setattr(c,'verify',lambda root:m)
    monkeypatch.setattr(c,'runner_environment',lambda port:{})
    monkeypatch.setattr(c.old.prior.offline.runtime,'inference_lease',lambda ports:nullcontext())
    monkeypatch.setattr(c.old,'adapter',lambda m:nullcontext())
    monkeypatch.setattr(c.grading,'load_manifest',lambda *a:{'tasks':{'old':{}}})
    monkeypatch.setattr(c.base,'heartbeat',lambda *a:None)
    called = []
    monkeypatch.setattr(c.v2,'step',lambda *a,**kw:called.append(a[2]))
    monkeypatch.setattr(c.signal,'signal',lambda *a:None)
    c.run(root)
    assert called == []
    status = read_json(root/'status.json')
    assert status['state'] == 'awaiting_environment'
    assert status['completed'] == 1 and status['new_model_calls_for_pending_task'] == 0


def test_solver_adapter_keeps_original_agent_and_caps():
    m = {'resilience_migration':{'grader':'/grader','grader_sha256':'abc'}}
    expected = c.old.prior.offline.ORIGINAL_COMMAND(Path('/task'),Path('/trials'),'skill-rsi',Path('/store'))
    with c.old.adapter(m):
        command = c.base.core.harbor_command(Path('/task'),Path('/trials'),'skill-rsi',Path('/store'))
    assert command[:len(expected)] == expected
    assert command[command.index('--agent-import-path')+1] == 'rsi_harness.clean_terminal_agent:CleanTerminalAgent'
    assert not any('BudgetAwareAgent' in x for x in command)


def test_report_compares_only_same_completed_tasks(tmp_path, monkeypatch):
    m = {'stream':[{'uid':'s0','task':'a'},{'uid':'s1','task':'b'}],
         'continuation':{'preserved_count':1,'added_count':1,'ready_grader_tasks':['b'],
                         'interpretation':'historical comparison, not equal total compute'}}
    atomic_json(tmp_path/'stream/s0/scored.json', {'reward':0})
    atomic_json(tmp_path/'matched-controls.json', {'common':[
        {'task':t,'controls':{'baseline':{'reward':1},'skill-rsi':{'reward':0}}} for t in ('a','b')]})
    monkeypatch.setattr(c.v2,'report',lambda *a:{})
    report = c.report(tmp_path,m)
    assert report['historical_matched_comparison']['n'] == 1
    assert report['historical_matched_comparison']['baseline_passed'] == 1
    assert report['continuation']['added_scored_tasks'] == 0
