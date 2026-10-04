"""Restart two idle, authorized TP4 groups with the exact prior generation argv."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import importlib.util
import io
import os
from pathlib import Path
import socket
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json,file_hash
from self_skill_rsi_dual import service
from . import campaign as c

TEMPLATE=Path('/data2/ytzhang/rsi-harness-runs/inference-dual-20261001/launch.json')
PROJECT=c.base.core.PROJECT


def launch(output):
    if output.exists():raise RuntimeError('new_service_launch_directory_required')
    rows=csv.reader(io.StringIO(subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.used','--format=csv,noheader,nounits'],text=True)))
    inventory={int(r[0]):(r[1].strip(),int(r[2])) for r in rows}
    if any(inventory[i][1]>128 for i in range(1,9)):raise RuntimeError('authorized_gpu_no_longer_free')
    processes=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader,nounits'],text=True)
    if any(r and r[0].strip() in {inventory[i][0] for i in range(1,9)} for r in csv.reader(io.StringIO(processes))):
        raise RuntimeError('authorized_gpu_has_compute_process')
    template=read_json(TEMPLATE)
    for port in c.PORTS:
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    output.mkdir(parents=True)
    launches=[]
    for port,cards in [(8041,'4,5,6,7'),(8040,'1,2,3,8')]:
        command=list(template['command']);command[command.index('--port')+1]=str(port)
        env=os.environ.copy()
        for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):env.pop(k,None)
        env.update({k:v for k,v in template['environment'].items() if k in service.INFRA_KEYS})
        env['CUDA_VISIBLE_DEVICES']=cards
        for key,name in [('TMPDIR','tmp'),('XDG_CACHE_HOME','cache')]:
            (output/name/str(port)).mkdir(parents=True);env[key]=str(output/name/str(port))
        log=output/f'service-{port}.log'
        with log.open('xb') as stream:
            child=subprocess.Popen(command,cwd=PROJECT,env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        value={'pid':child.pid,'identity':c.base.core.process_identity(child.pid),'port':port,'cards':cards,
            'command':command,'log':str(log),'time':time.time(),'template':str(TEMPLATE),'template_sha256':file_hash(TEMPLATE)}
        atomic_json(output/f'launch-{port}.json',value);launches.append(value)
    atomic_json(output/'launches.json',{'time':time.time(),'services':launches})


def accept(output):
    spec=importlib.util.spec_from_file_location('inference_check',PROJECT/'scripts/check-rsi-inference.py')
    checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
    launches=read_json(output/'launches.json')['services']
    deadline=time.monotonic()+900
    while not all(service.endpoint(s['port'])['healthy'] for s in launches):
        if any(not c.base.legacy.alive(s) for s in launches):raise RuntimeError('model_service_exited')
        if time.monotonic()>deadline:raise RuntimeError('model_service_startup_timeout')
        time.sleep(5)
    def check(s):
        service.owned(s)
        result=checker.check(s['port'])
        atomic_json(output/f"smoke-{s['port']}.json",result)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(check,launches))
    paths=[TEMPLATE,PROJECT/'scripts/check-rsi-inference.py',Path('/data3/ytzhang/models/Qwen3.8-27B/generation_config.json')]
    paths+=[output/f'{prefix}-{port}.json' for prefix in ('launch','smoke') for port in c.PORTS]
    atomic_json(output/'deployment.json',{'time':time.time(),'ports':c.PORTS,'gpu_groups':c.GROUPS,
        'services':launches,'model':'Qwen/Qwen3.8-27B','functional_checks_passed':True,
        'generation_configuration_changed':False,'evidence_hashes':{str(p):file_hash(p) for p in paths}})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--launch',action='store_true');p.add_argument('--accept',action='store_true');a=p.parse_args()
    if a.launch:launch(a.output.resolve())
    if a.accept:accept(a.output.resolve())
