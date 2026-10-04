"""Cached image preparation and no-model acceptance of the actual agent runtime."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

from harbor.models.task.task import Task
from harbor.environments.base import environment_content_hash
from offline_verifier.runner import docker
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import PROJECT, atomic_json, file_hash
from terminal_offline_expand.assets import materialize


def image_id(name):
    r = subprocess.run(['docker','image','inspect',name,'--format','{{.Id}}'],capture_output=True,text=True,timeout=20)
    return r.stdout.strip() if r.returncode == 0 else None


def resolve_entry(entry, infra):
    root = infra.parent
    resolved = materialize(root, entry)
    path = Path(entry['path']); task = Task(path)
    image = task.config.environment.docker_image or 'hb__' + environment_content_hash(path/'environment', docker_image=None) + ':rsi-cache'
    current = image_id(image)
    journal = read_json(root/'asset-materialization'/(entry['task']+'.json'))
    # Rebuilt images with explicit official tags must not retain an LFS placeholder.
    rebuild = False
    dockerfile = (path/'environment/Dockerfile').read_text()
    for rel, blob in journal['blobs'].items():
        if not current or not rel.startswith('environment/'): continue
        source = rel[len('environment/'):]
        match = re.search(r'^COPY\s+' + re.escape(source) + r'\s+(/\S+)\s*$', dockerfile, re.M)
        if not match: raise RuntimeError('asset_copy_mapping_requires_review:'+rel)
        target = match[1] + (Path(source).name if match[1].endswith('/') else '')
        check = subprocess.run(['docker','run','--rm','--network','none','--entrypoint','sha256sum',current,target],
                               capture_output=True,text=True,timeout=30)
        if check.returncode or check.stdout.split()[0] != blob['oid']: rebuild = True
    if not current or rebuild:
        output = root/'image-preparation'/entry['task']; output.mkdir(parents=True,exist_ok=True)
        for base in re.findall(r'^FROM\s+(\S+)',dockerfile,re.M):
            if base == 'scratch': continue
            if not image_id(base): raise RuntimeError('missing_build_base:'+base)
        if current:
            backup = 'rsi-recovery-backup/' + entry['task'].lower() + ':' + current[7:19]
            docker(['tag',current,backup])
        args=['docker','build','--network','host','--tag',image]
        for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
            args += ['--build-arg',key+'=http://10.0.2.2:17898']
        args += [str(path/'environment')]
        attempt = output / (str(time.time_ns())+'.log')
        with attempt.open('xb') as log:
            subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=2400)
        current=image_id(image)
        atomic_json(output/'result.json',{'image':image,'image_id':current,'previous_image_id':image_id(backup) if rebuild else None,
            'task_checksum':resolved['task_checksum'],'log':str(attempt),'model_calls':0})
    if not current: raise RuntimeError('missing_prepared_image')
    info=json.loads(docker(['image','inspect',current]).stdout)[0]
    return {k:v for k,v in {**resolved,'image':image,'image_id':current,'readiness_root':str(infra),
        'working_directory':info['Config'].get('WorkingDir') or '/'}.items() if k!='old_ticket'}


def runtime_preflight(root, entry, grader, grader_hash, output):
    receipt = output/'runtime-acceptance'/entry['task']/'receipt.json'
    if receipt.exists():
        value=read_json(receipt)
        if value['grader_sha256']!=grader_hash or value['image_id']!=entry['image_id']: raise RuntimeError('runtime_acceptance_inputs_changed')
        return value
    folder=receipt.parent/str(time.time_ns()); folder.mkdir(parents=True)
    args=[str(PROJECT/'.venv-terminal/bin/harbor'),'trials','start','--path',entry['path'],'--trials-dir',str(folder/'trials'),
        '--agent-import-path','rsi_harness.terminal_smoke_agent:TerminalSmokeAgent','--agent-timeout','120','--verifier-timeout','600',
        '--verifier','terminal_offline_recover.harbor:RecoveryVerifier','--verifier-kwarg','manifest='+str(grader),
        '--verifier-kwarg','manifest_sha256='+grader_hash]
    with (folder/'smoke.log').open('xb') as log:
        subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900)
    paths=list((folder/'trials').glob('*/result.json'))
    if len(paths)!=1: raise RuntimeError('runtime_preflight_missing_result')
    data=read_json(paths[0]); runtime=read_json(paths[0].parent/'agent/infrastructure.json',{})
    if data.get('exception_info') or runtime.get('tmux')!='passed' or image_id(entry['image'])!=entry['image_id']:
        raise RuntimeError('runtime_preflight_failed:'+str(data.get('exception_info')))
    value={'passed':True,'task':entry['task'],'model_calls':0,'grader_sha256':grader_hash,'image_id':entry['image_id'],
        'result':str(paths[0]),'result_sha256':file_hash(paths[0]),'runtime':runtime,'time':time.time()}
    atomic_json(receipt,value); return value
