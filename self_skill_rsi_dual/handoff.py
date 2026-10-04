"""Finish the current step, then hand over automatically without model replay."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json,file_hash
from self_skill_rsi_resilient.guardian import endpoint
from . import campaign as c,guardian,service


def boundary_action(state,controller_live,children_live):
    if state in ('needs_attention','complete','awaiting_environment'):
        return 'stop_without_expansion'
    if controller_live or children_live:
        return 'wait'
    return 'migrate' if state == 'paused' else 'unexpected_stop'


def deployment(source,inference):
    old = c.PRIOR_VERIFY(source)
    original = Path(old['inference_migration']['deployment'])
    d = read_json(original)
    retained = next(s for s in d['services'] if s['port'] == 8041)
    new_path = inference/'launch.json'
    added = read_json(new_path)
    old_argv,_ = service.owned(retained)
    new_argv,_ = service.owned(added)
    adjusted = list(new_argv)
    adjusted[adjusted.index('--port')+1] = '8041'
    if adjusted != old_argv:
        raise RuntimeError('model_configuration_changed_beyond_port')
    smoke = inference/'smoke-8040.json'
    if read_json(smoke,{}).get('passed') is not True:
        raise RuntimeError('new_service_smoke_missing')
    if not all(endpoint(p)['healthy'] and endpoint(p)['idle'] for p in c.PORTS):
        raise RuntimeError('handoff_requires_healthy_idle_endpoints')
    value = {'time':time.time(),'ports':c.PORTS,'gpu_groups':c.GROUPS,
        'services':[retained,added],'model':old['model'],'functional_checks_passed':True,
        'generation_configurations_identical':True,
        'evidence_hashes':{**d['evidence_hashes'],str(original):file_hash(original),
            str(new_path):file_hash(new_path),str(smoke):file_hash(smoke)}}
    path = inference/'deployment.json'
    if path.exists():
        raise RuntimeError('deployment_exists; inspect_before_retry')
    atomic_json(path,value)
    return path


def run(source,target,inference,deadline=36000):
    with c.base.legacy.exclusive(inference/'handoff.lock'):
        started = time.time()
        while True:
            c.PRIOR_VERIFY(source)
            status = read_json(source/'status.json',{})
            alive = c.base.legacy.alive(read_json(source/'supervisor_launch.json',{}))
            children = [str(p) for p in source.glob('stream/**/child.json')
                        if c.base.legacy.alive(read_json(p))]
            action = boundary_action(status.get('state'),alive,bool(children))
            atomic_json(inference/'handoff-status.json',{'time':time.time(),'started':started,
                'state':action,'source':str(source),'target':str(target),
                'source_status':status,'source_controller_alive':alive,'native_children':children,
                'formal_replays':0})
            if action == 'migrate':
                break
            if action != 'wait' or time.time()-started > deadline:
                raise RuntimeError('cannot_handoff_at_boundary:'+action+':'+str(status))
            time.sleep(10)
        unit = read_json(source/'guardian-service.json')['unit']
        expected = 'rsi-self-skill-continue-'+source.name+'.service'
        if unit != expected:
            raise RuntimeError('unexpected_source_guardian_unit')
        subprocess.run(['systemctl','--user','stop',unit],check=True,timeout=30)
        d = deployment(source,inference)
        m = c.prepare(target,source,d)
        guardian.install(target)
        atomic_json(inference/'handoff-status.json',{'time':time.time(),'state':'complete',
            'source':str(source),'target':str(target),'ports':c.PORTS,'gpu_groups':c.GROUPS,
            'preserved_scores':m['dual_inference_migration']['preserved_count'],
            'next_task':m['dual_inference_migration']['first_new_task'],'formal_replays':0})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--target',type=Path,required=True)
    p.add_argument('--inference',type=Path,required=True)
    a = p.parse_args()
    try:
        run(a.source.resolve(),a.target.resolve(),a.inference.resolve())
    except Exception as exc:
        atomic_json(a.inference/'handoff-error.json',{'time':time.time(),
            'reason':type(exc).__name__+':'+str(exc),'formal_replays':0})
        # No model retry. Release only the new, still-idle replica if it was
        # never handed over to a live target controller/guardian.
        target_live = any(c.base.legacy.alive(read_json(a.target/name,{})) for name in
                          ('supervisor_launch.json','watchdog_launch.json'))
        added = read_json(a.inference/'launch.json')
        if added and not target_live:
            service.stop(added,a.inference/'released-after-handoff-error.json')
        raise
