"""Prepare, launch and inspect the added ReasoningBank arm using existing calibrated controls."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from _bootstrap import REPO
from rsi_portable.paths import WORKSPACE, RUNTIME
from swe_chain_evo.common import atomic, read, sha, docker, docker_env


def prepare(campaign, inventory, source_workspace, port=19400, docker_hosts=None, embedding_proxy=''):
    from bootstrap import bootstrap, link
    from harbor.models.task.task import Task
    from rsi_portable.reproducibility import verify
    from reasoning_bank import COMMIT, VENDOR
    from reasoning_bank.native import source_evidence
    from methods.evo_fifty.spend import SpendLedger
    bootstrap()
    link(REPO/'src/agents/reasoning_bank',WORKSPACE/'reasoning_bank')
    if subprocess.check_output(['git','-C',str(VENDOR),'rev-parse','HEAD'],text=True).strip()!=COMMIT:
        raise RuntimeError('Official ReasoningBank revision mismatch')
    if subprocess.check_output(['git','-C',str(VENDOR),'status','--porcelain'],text=True).strip():
        raise RuntimeError('Official ReasoningBank source modified')
    parity=verify()
    if not parity['passed']:
        raise RuntimeError('Original common runtime parity failed: '+str(parity['errors']))
    campaign=Path(campaign).resolve()
    if (campaign/'RUN_AUTHORIZATION.json').exists():
        raise RuntimeError('Authorized campaign preparation is immutable')
    plan=read(inventory)
    source=Path(plan['chains'][0]['methods']['kira-static']['root'])
    baseline=read(source/'configuration.json')
    method=read(REPO/'configs/methods/reasoning-bank.json')
    common=read(REPO/'configs/deepseek/model.json')
    for field in ('solve','accounting','chain_protocol'):
        if baseline[field]!=common[field]:
            raise RuntimeError('Historical baseline common protocol differs: '+field)
    SpendLedger(campaign/'spend.sqlite','50.00',allow_uncertain=True)
    # Reuse only public dataset metadata and private frozen test mappings. Never copy learned artifacts/results.
    data=Path(source_workspace).resolve()/'data/SWE-Chain-Evo'
    link(data,WORKSPACE/'data/SWE-Chain-Evo')
    old_release=Path(source_workspace).resolve()
    profiles=[]
    for i,chain in enumerate(plan['chains']):
        name=chain['chain']; source=Path(chain['methods']['kira-static']['root'])
        cfg=copy.deepcopy(read(source/'configuration.json'))
        stream=read(source/'task-stream.json')
        if len(stream)!=15 or [e['version_chain_index'] for e in stream]!=list(range(15)):
            raise RuntimeError('Incomplete chain '+name)
        for field in ('model','solve','accounting','chain_protocol'):
            if cfg[field]!=baseline[field]:
                raise RuntimeError('Baseline chain contract differs: '+name+'/'+field)
        images={e['image_id'] for e in stream}
        environment=docker_env()
        host=(docker_hosts or {}).get(name,environment.get('DOCKER_HOST',''))
        environment['DOCKER_HOST']=host
        inspected=subprocess.run(['docker','image','inspect',*sorted(images),'--format','{{.Id}}'], env=environment,capture_output=True,text=True,timeout=60,check=True)
        actual=set(inspected.stdout.splitlines())
        if images!=actual:
            raise RuntimeError('Missing or changed solver images: '+name)
        root=campaign/'repeat-00'/name;root.mkdir(parents=True,exist_ok=True)
        ready=[]
        for j,e in enumerate(stream):
            if Task(Path(e['path'])).checksum!=e['task_checksum']:
                raise RuntimeError('Prepared task changed: '+e['task'])
            mapping=old_release/'runs/grader-preflight'/e['task']/'test-id-map.json'
            frozen=read(mapping)
            if frozen['normalizer_sha256']!=sha(WORKSPACE/'formal_swe_chain/grading.py'):
                raise RuntimeError('Grader normalizer changed')
            if frozen['instance_sha256']!=sha(data/e['instance_path']):
                raise RuntimeError('Private task map metadata mismatch')
            target=WORKSPACE/'runs/grader-preflight'/e['task']/'test-id-map.json'
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(mapping,target)
            atomic(root/'entries'/f'{j}.json',e)
            ready.append({'task':e['task'],'task_checksum':e['task_checksum'],'image_id':e['image_id'],
                          'test_id_map':str(target),'test_id_map_sha256':sha(target)})
        cfg['arms']={'reasoning-bank':method}
        cfg['spend']={'global_limit':'50.00','ledger_path':str(campaign/'spend.sqlite'),
                      'scope':'one key, all seven chains and all repeats; includes paid preflight and embeddings'}
        cfg['credential_file']=str(campaign/'.private/openrouter.key')
        cfg['session_root']=str(campaign)
        cfg['runtime'].update(gateway_port=port+i,model_http_proxy='',host_outbound_proxy='',docker_host=host,
                              embedding_http_proxy=embedding_proxy)
        cfg['comparison_source']={'run':str(source),'configuration_sha256':sha(source/'configuration.json'),
                                  'historical_metric_status':chain['metric_status']}
        cfg['method_profile']='official-reasoningbank-kira-per-task-v1'
        cfg['initial_state_policy']='empty reasoning bank and embeddings per chain/repeat'
        atomic(root/'configuration.json',cfg);atomic(root/'task-stream.json',stream)
        atomic(root/'memory.json',[])
        shutil.copy2(source/'tokenizer.json',root/'tokenizer.json')
        atomic(root/'readiness.json',{'passed':True,'paid_model_calls':0,'tasks':ready,
              'common_runtime_parity':parity,'official_sources':source_evidence(),
              'reuse':'same calibrated immutable images/tasks/test maps; no past answers or learning state'})
        files=[root/'configuration.json',root/'task-stream.json',root/'tokenizer.json',root/'readiness.json']
        files+=list((root/'entries').glob('*.json'))+[Path(r['test_id_map']) for r in ready]
        files+=list((REPO/'src/agents/reasoning_bank').glob('*.py'))+[REPO/'configs/methods/reasoning-bank.json',REPO/'scripts/reasoning_bank.py',REPO/'scripts/reasoning_bank_watch.py']
        files+=[VENDOR/p for p in source_evidence()]
        reference=read(REPO/'upstream/reproduction-reference.json')
        files += [REPO/p for p in reference['files']]
        atomic(root/'source-pins.json',{str(p):sha(p) for p in files})
        profiles.append({'chain':name,'root':str(root),'control':str(source),'tasks':15,
                         'historical_metric_status':chain['metric_status']})
    atomic(campaign/'manifest.json',{'schema':'reasoning-bank-seven-chain-v1','runs':profiles,
          'preparation_paid_model_calls':0,'limit_usd':50,'method':method,'repeat_policy':'additional independent repeats while authorized shared balance permits'})
    print(json.dumps({'prepared_chains':len(profiles),'tasks':105,'paid_calls':0,'campaign':str(campaign)},indent=2))


def authorize(campaign):
    manifest=read(campaign/'manifest.json')
    roots=[Path(r['root']) for r in manifest['runs']]
    if not (campaign/'.private/openrouter.key').is_file():
        raise RuntimeError('Private authorized key is not configured')
    atomic(campaign/'RUN_AUTHORIZATION.json',{'authorized':True,'user_instruction':'Run ReasoningBank on all seven chains using the provided key; aggregate budget up to $50',
          'configuration_hashes':{str(r):sha(r/'configuration.json') for r in roots},'time':time.time()})


def launch(campaign, chain=None, pilot_tasks=None):
    from rsi_portable.reproducibility import verify
    parity=verify()
    if not parity['passed']:
        raise RuntimeError('Common runtime parity failed: '+str(parity['errors']))
    manifest=read(campaign/'manifest.json')
    authorize(campaign)
    workers=[]
    for row in manifest['runs']:
        if chain and row['chain']!=chain:continue
        root=Path(row['root'])
        import fcntl
        with (root/'.campaign.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
        state=read(root/'native-campaign.json') if (root/'native-campaign.json').exists() else {}
        if state.get('state') in ('completed','needs_review','budget_exhausted'):continue
        command=[sys.executable,'-u','-m','reasoning_bank.campaign','--root',str(root)]
        if pilot_tasks:command+=['--pilot-tasks',str(pilot_tasks)]
        env=dict(os.environ,PYTHONPATH=str(WORKSPACE)+os.pathsep+str(RUNTIME),PYTHONDONTWRITEBYTECODE='1',LITELLM_LOCAL_MODEL_COST_MAP='True',DOCKER_HOST=read(root/'configuration.json')['runtime']['docker_host'])
        with (root/'launcher.log').open('ab') as log:
            p=subprocess.Popen(command,cwd=WORKSPACE,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        workers.append({'chain':row['chain'],'pid':p.pid})
    print(json.dumps({'started':workers,'pilot_tasks':pilot_tasks},indent=2))


def status(campaign):
    from methods.evo_fifty.spend import SpendLedger
    from reasoning_bank.campaign import report
    m=read(campaign/'manifest.json');rows=[]
    for row in m['runs']:
        root=Path(row['root']); state=root/'native-campaign.json'
        rows.append(report(root,read(state),persist=False) if state.exists() else {'chain':row['chain'],'completed':0,'status':'prepared'})
    value={'chains':rows,'spend':SpendLedger(campaign/'spend.sqlite','50.00',allow_uncertain=True).snapshot()}
    atomic(campaign/'progress.json',value)
    print(json.dumps(value,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','start','status','pause'])
    p.add_argument('--campaign',type=Path,required=True);p.add_argument('--inventory',type=Path)
    p.add_argument('--source-workspace',type=Path);p.add_argument('--chain-docker-host',action='append',default=[]);p.add_argument('--chain');p.add_argument('--pilot-tasks',type=int);p.add_argument('--port',type=int,default=19400);p.add_argument('--embedding-proxy',default='')
    a=p.parse_args()
    if a.action=='prepare':prepare(a.campaign,a.inventory,a.source_workspace,a.port,dict(x.split('=',1) for x in a.chain_docker_host),a.embedding_proxy)
    elif a.action=='start':launch(a.campaign,a.chain,a.pilot_tasks)
    elif a.action=='status':status(a.campaign)
    else:(a.campaign/'HOLD').write_text('User requested pause.\n')
