"""Checkpointed Evo campaign with native method loops and one dollar ledger."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import json
import os
import secrets
import subprocess
from pathlib import Path
import shutil
import threading
import time
import traceback

from methods.evo_agents import VENDOR_AGENTS
from methods.evo_trial import PROJECT, sha
from .gateway import Broker, Server, load_config
from .trial import TrialRunner, atomic, NetworkUnavailable
from .spend import SpendBlocked
from .report import ARMS, report
from .native_meta import VENDOR


class UpdateFailure(RuntimeError):
    pass


def eligible_actions(state, active, total_tasks, max_lead=2):
    """Preserve each arm's causal order, with a bounded lead over the cohort."""
    prefix=min(len(state['arms'][arm]['rows']) for arm in ARMS)
    for arm in ARMS:
        item=state['arms'][arm]
        if arm in active or item.get('status') in ('needs_review','budget_exhausted'):
            continue
        if item.get('retry_not_before',0)>time.time():
            continue
        if item['pending_update'] is not None:
            yield arm,'rsi_update',item['pending_update']
        elif len(item['rows'])<total_tasks and len(item['rows'])<prefix+max_lead:
            yield arm,'formal',len(item['rows'])


def update_subprocess(runner, arm, index, control_token):
    output=runner.root/'arms'/arm/'steps'/f'{index:06d}'/'update'
    output.mkdir(parents=True,exist_ok=True)
    outcome=output/'worker-outcome.json'
    if outcome.exists():
        destination=output/f'worker-outcome-before-{time.time_ns()}.json'
        shutil.copy2(outcome,destination)
        outcome.unlink()
    env=dict(runner.env)
    for name in ('OPENROUTER_API_KEY','DEEPSEEK_API_KEY','ANTHROPIC_API_KEY','OPENAI_API_KEY'):
        env.pop(name,None)
    env.update(EVO_ONLINE_CONTROL_TOKEN=control_token,PYTHONPATH=str(PROJECT),
               TMPDIR=str(runner.root/'tmp'),PYTHONDONTWRITEBYTECODE='1')
    cmd=[str(PROJECT/'.venv-terminal/bin/python'),'-u','-m','methods.evo_smoke.update_worker',
         '--root',str(runner.root),'--arm',arm,'--index',str(index)]
    with (output/'coordinator-update-worker.log').open('ab') as log:
        process=subprocess.Popen(cmd,cwd=PROJECT,env=env,stdin=subprocess.DEVNULL,
                                 stdout=log,stderr=subprocess.STDOUT)
        atomic(output/'worker-launch.json',{'pid':process.pid,'time':time.time(),
                                          'arm':arm,'index':index,'isolated_hooks':True})
        process.wait()
    if not outcome.exists():raise UpdateFailure('update worker exited without a checkpoint; inspect its log')
    data=json.loads(outcome.read_text())
    if not data['ok']:
        error=data['error']
        if error['type']=='NetworkUnavailable':
            raise NetworkUnavailable(error['message'])
        if error['type']=='SpendBlocked' or 'global dollar limit' in error['message']:
            raise SpendBlocked(error['message'])
        raise UpdateFailure(error['message'])
    return data['decision']


def source_pins(root):
    paths=list((PROJECT/'methods/evo_online').glob('*.py')) + list((PROJECT/'methods/evo_smoke').glob('*.py'))
    paths += [PROJECT/'methods/evo_agents.py',PROJECT/'methods/evo_trial.py',PROJECT/'methods/modular_update.py',
              root/'configuration.json',root/'task-stream.json']
    for repo in (PROJECT/'methods/vendor/meta-harness',PROJECT/'methods/vendor/ModularRSI'):
        import subprocess
        status=subprocess.check_output(['git','-C',str(repo),'status','--short'],text=True).strip()
        if status:raise RuntimeError('official tracked source modified: '+str(repo))
        tracked=subprocess.check_output(['git','-C',str(repo),'ls-files'],text=True).splitlines()
        paths += [repo/p for p in tracked if p.endswith(('.py','.sh','.md','.txt','.json')) and not p.startswith('trajectories/')]
    return {str(p):sha(p) for p in paths if p.is_file()}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();root=args.root
    lock=(root/'.native-campaign.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    broker=Broker(root,(root/'.openrouter-key').read_text().strip(),load_config(root))
    runner=TrialRunner(root,broker,port=broker.config['runtime']['gateway_port'])
    if not json.loads((root/'native-cli-real-smoke/result.json').read_text())['exit_code']==0:
        raise RuntimeError('native real CLI smoke not passed')
    if not json.loads((root/'native-modular-baseline-smoke.json').read_text())['passed']:
        raise RuntimeError('native module library smoke not passed')
    pins_path=root/'native-source-pins.json'
    pins=source_pins(root)
    if pins_path.exists():
        if json.loads(pins_path.read_text())!=pins:raise RuntimeError('campaign source differs from checkpoint')
    else:
        atomic(pins_path,pins)
        shutil.copytree(PROJECT/'methods/evo_smoke',root/'native-adapter-snapshot',ignore=shutil.ignore_patterns('__pycache__'))
    runner.pins=pins
    initial=root/'initial/modular/gen_0/modules'
    if not initial.exists():
        shutil.copytree(VENDOR_AGENTS/'terminus_2_modular/modules',initial,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    from methods.modular_update import tree_hashes
    assert tree_hashes(initial)==tree_hashes(VENDOR_AGENTS/'terminus_2_modular/modules')
    total_tasks=min(len(runner.stream), runner.config['runtime'].get('formal_task_limit',len(runner.stream)))
    state_path=root/'native-campaign.json'
    if state_path.exists():state=json.loads(state_path.read_text())
    else:
        state={'schema':'native-evo-campaign-v1','created':time.time(),'state':'starting',
               'configuration_sha256':sha(root/'configuration.json'),
               'initial_pairing':{'meta_harness_sha256':sha(VENDOR/'agents/baseline_kira.py'),
                                  'modular_library':tree_hashes(initial)},
               'arms':{a:{'rows':[],'pending_update':None,'parent':str(initial) if 'modular' in a else str(VENDOR/'agents/baseline_kira.py'),
                          'candidate':None} for a in ARMS}}
        atomic(state_path,state)
    control={arm:secrets.token_urlsafe(32) for arm in ('meta-harness','modularrsi')}
    server=Server(('127.0.0.1',runner.port),broker,{token:arm for arm,token in control.items()})
    threading.Thread(target=server.serve_forever,daemon=True).start()
    stop_event=threading.Event()
    def heartbeat():
        while not stop_event.wait(20):
            atomic(root/'heartbeat.json',{'pid':os.getpid(),'time':time.time(),'spend':broker.ledger.snapshot()})
    threading.Thread(target=heartbeat,daemon=True).start()
    state.setdefault('active',{})
    state['active']={}
    def save():
        state.update(state='running',phase='parallel',active_arm=None,active_index=None,
                     pid=os.getpid(),updated=time.time(),spend=broker.ledger.snapshot())
        atomic(state_path,state)
        atomic(root/'status.json',{k:state[k] for k in ('state','phase','active','pid','updated','spend')})
    try:
        futures={}
        concurrency=runner.config['runtime'].get('arm_concurrency',1)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while True:
                paused=(root/'PAUSE').exists()
                if not paused:
                    actions=eligible_actions(state,set(state['active']),total_tasks,
                                              runner.config['runtime'].get('max_task_lead',2))
                    for arm,phase,index in actions:
                        if len(futures)>=concurrency:break
                        item=state['arms'][arm]
                        state['active'][arm]={'phase':phase,'index':index,'started':time.time()}
                        save()
                        if phase=='rsi_update':
                            future=pool.submit(update_subprocess,runner,arm,index,control[arm])
                        else:
                            output=root/'arms'/arm/'steps'/f'{index:06d}'/'formal'
                            future=pool.submit(runner.run,arm,index,output,
                                modules=Path(item['parent']) if 'modular' in arm else None,
                                candidate=item['candidate'])
                        futures[future]=(arm,phase,index)
                if not futures:
                    if not paused and any(item.get('status')=='waiting_for_network' for item in state['arms'].values()):
                        # Stay alive while all arms are waiting for a transport
                        # retry. Heartbeats continue and no paid calls are sent.
                        time.sleep(1)
                        continue
                    complete=all(len(item['rows'])==total_tasks and item['pending_update'] is None
                                 for item in state['arms'].values())
                    state['state']='paused_at_checkpoint' if paused else 'completed' if complete else (
                        'budget_exhausted' if any(i.get('status')=='budget_exhausted' for i in state['arms'].values())
                        else 'needs_review')
                    break
                done,_=wait(futures,timeout=1,return_when=FIRST_COMPLETED)
                for future in done:
                    arm,phase,index=futures.pop(future)
                    item=state['arms'][arm]
                    state['active'].pop(arm)
                    try:
                        result=future.result()
                        if phase=='formal':
                            item['rows'].append(result)
                            every=runner.config['arms'].get(arm,{}).get('update_every_completed_tasks',0)
                            if every and len(item['rows'])%every==0:item['pending_update']=index
                        else:
                            if result['candidate']:
                                item['parent']=result['candidate']
                                if arm=='meta-harness':item['candidate']=result['candidate']
                            item['pending_update']=None
                            item['resume_attempts']=0
                        item['status']='ready'
                        item.pop('error',None)
                        item.pop('retry_not_before',None)
                    except BaseException as exc:
                        item['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
                        recoverable=(arm=='meta-harness' and phase=='rsi_update'
                                     and 'native proposer provider accounting requires review' in str(exc)
                                     and item.get('resume_attempts',0)<runner.config['runtime'].get('native_session_resume_limit',2))
                        if isinstance(exc,NetworkUnavailable):
                            item['status']='waiting_for_network'
                            item['retry_not_before']=time.time()+runner.config['runtime'].get('network_recovery_cooldown_seconds',60)
                        elif recoverable:
                            item['resume_attempts']=item.get('resume_attempts',0)+1
                            item['status']='ready'
                        else:
                            item['status']='budget_exhausted' if isinstance(exc,SpendBlocked) else 'needs_review'
                        print(json.dumps({'arm':arm,'phase':phase,'state':item['status'],
                                          'error':str(exc)},ensure_ascii=False),flush=True)
                    save()
                    report(root,state,broker)
    except BaseException as exc:
        state.update(state='budget_exhausted' if isinstance(exc,SpendBlocked) else 'needs_review',
                     error={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
        print(json.dumps({'state':state['state'],'error':state['error']['message']},ensure_ascii=False),flush=True)
    finally:
        state['updated']=time.time();state['spend']=broker.ledger.snapshot();atomic(state_path,state)
        atomic(root/'status.json',{k:v for k,v in state.items() if k!='arms'})
        report(root,state,broker)
        stop_event.set();server.shutdown();server.server_close()
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close()


if __name__=='__main__':main()
