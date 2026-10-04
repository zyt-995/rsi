import json
from pathlib import Path
import signal

import pytest

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_dual import campaign as c,service,handoff


@pytest.mark.parametrize('state,alive,children,expected',[
    ('running',True,True,'wait'),('paused',True,False,'wait'),
    ('paused',False,True,'wait'),('paused',False,False,'migrate'),
    ('needs_attention',False,False,'stop_without_expansion'),
    ('complete',False,False,'stop_without_expansion'),
    ('awaiting_environment',False,False,'stop_without_expansion'),
    ('running',False,False,'unexpected_stop')])
def test_boundary_does_not_interrupt_or_retry(state,alive,children,expected):
    assert handoff.boundary_action(state,alive,children) == expected


def test_gpu_targets_exclude_other_users_and_reserved_card():
    assert service.ALLOWED == {8040:'1,2,3,8',8041:'4,5,6,7'}
    assert {g for group in c.GROUPS for g in group} == set(range(1,9))
    assert c.PORTS[0] == 8041  # Keep the official solver on its original replica.


def test_busy_compute_gpu_is_rejected_even_with_low_memory(monkeypatch):
    inventory = '\n'.join(f'{i}, gpu-{i}, 18' for i in range(10))
    def output(args,**kwargs):
        return inventory if '--query-gpu=index,uuid,memory.used' in args else 'gpu-8, 1234\n'
    monkeypatch.setattr(service.subprocess,'check_output',output)
    with pytest.raises(RuntimeError,match='compute_process'):
        service.free_cards('1,2,3,8')


def test_free_gpu_inventory_does_not_claim_gpu_zero(monkeypatch):
    inventory = '\n'.join(f'{i}, gpu-{i}, {16473 if i==0 else 18}' for i in range(10))
    monkeypatch.setattr(service.subprocess,'check_output',lambda args,**kw:
        inventory if '--query-gpu=index,uuid,memory.used' in args else 'gpu-0, 1234\n')
    assert set(service.free_cards('1,2,3,8')) == {'1','2','3','8'}


def test_release_requires_idle_exact_owned_process(tmp_path,monkeypatch):
    s = {'pid':123,'identity':'abc','port':8040,'cards':'1,2,3,8'}
    calls = []
    monkeypatch.setattr(service.prior.base.legacy,'alive',lambda _:True)
    monkeypatch.setattr(service,'owned',lambda _:calls.append('owned'))
    monkeypatch.setattr(service,'endpoint',lambda _:{'healthy':True,'idle':False})
    monkeypatch.setattr(service.os,'kill',lambda *args:calls.append(args))
    assert not service.stop(s,tmp_path/'receipt.json')
    assert calls == ['owned']
    monkeypatch.setattr(service,'endpoint',lambda _:{'healthy':True,'idle':True})
    assert service.stop(s,tmp_path/'receipt.json')
    assert calls[-1] == (123,signal.SIGTERM)
    assert read_json(tmp_path/'receipt.json')['service'] == s


def test_running_adapter_reuses_existing_controller_and_restores_globals(monkeypatch,tmp_path):
    original_verify,original_report = c.prior.verify,c.prior.report
    seen = []
    def run(root):
        seen.append((root,c.prior.verify,c.prior.report))
    monkeypatch.setattr(c.prior,'run',run)
    c.run(tmp_path)
    assert seen == [(tmp_path,c.verify,c.report)]
    assert c.prior.verify is original_verify and c.prior.report is original_report
