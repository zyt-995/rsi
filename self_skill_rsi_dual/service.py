"""Exact owned-service operations; GPU 0 and GPU 9 are never targets."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_continue import campaign as prior
from self_skill_rsi_resilient.guardian import endpoint

ALLOWED = {8040: '1,2,3,8', 8041: '4,5,6,7'}
INFRA_KEYS = ('CUDA_HOME', 'NO_PROXY', 'no_proxy', 'HF_HUB_OFFLINE', 'HF_DATASETS_OFFLINE',
              'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'DO_NOT_TRACK',
              'VLLM_NO_USAGE_STATS', 'VLLM_USE_FLASHINFER_SAMPLER')


def owned(service):
    pid = service['pid']
    proc = Path(f'/proc/{pid}')
    if (not prior.base.legacy.alive(service) or proc.stat().st_uid != os.getuid()):
        raise RuntimeError('service_not_live_owned_identity')
    argv = proc.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
    env = dict(x.split('=', 1) for x in proc.joinpath('environ').read_bytes().decode().split('\0') if '=' in x)
    port, cards = service['port'], service['cards']
    if (ALLOWED.get(port) != cards or env.get('CUDA_VISIBLE_DEVICES') != cards
            or 'vllm.entrypoints.openai.api_server' not in argv
            or argv[argv.index('--port')+1] != str(port)):
        raise RuntimeError('service_configuration_not_registered')
    return argv, env


def free_cards(cards):
    rows = csv.reader(io.StringIO(subprocess.check_output(['nvidia-smi',
        '--query-gpu=index,uuid,memory.used','--format=csv,noheader,nounits'],text=True)))
    inventory = {int(r[0]): (r[1].strip(),int(r[2])) for r in rows}
    selected = {int(c) for c in cards.split(',')}
    if selected != {1,2,3,8} or any(inventory[i][1] > 128 for i in selected):
        raise RuntimeError('new_gpu_group_not_free')
    used = subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid',
                                   '--format=csv,noheader,nounits'],text=True)
    uuids = {inventory[i][0] for i in selected}
    if any(row and row[0].strip() in uuids for row in csv.reader(io.StringIO(used))):
        raise RuntimeError('new_gpu_group_has_compute_process')
    return {str(i):{'uuid':inventory[i][0],'memory_mib':inventory[i][1]} for i in sorted(selected)}


def launch(output, source_deployment):
    output = output.resolve()
    with prior.base.legacy.exclusive(output/'launch.lock'):
        if (output/'launch.json').exists():
            raise RuntimeError('launch_already_recorded; no_blind_relaunch')
        source = read_json(source_deployment)
        template = next(s for s in source['services'] if s['port'] == 8041)
        argv, source_env = owned(template)
        if (argv[argv.index('--tensor-parallel-size')+1] != '4'
                or argv[argv.index('--model')+1] != '/data3/ytzhang/models/Qwen3.8-27B'):
            raise RuntimeError('unexpected_template_model_or_parallelism')
        free = free_cards('1,2,3,8')
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',8040))
        command = list(argv)
        command[command.index('--port')+1] = '8040'
        env = os.environ.copy()
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            env.pop(key,None)
        env.update({k:source_env[k] for k in INFRA_KEYS if k in source_env})
        env['CUDA_VISIBLE_DEVICES'] = '1,2,3,8'
        for key, name in [('TMPDIR','tmp'),('XDG_CACHE_HOME','cache')]:
            (output/name).mkdir(exist_ok=True)
            env[key] = str(output/name)
        owned(template)
        log = output/'service.log'
        with log.open('xb') as stream:
            child = subprocess.Popen(command,cwd=prior.base.core.PROJECT,env=env,
                stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        value = {'pid':child.pid,'identity':prior.base.core.process_identity(child.pid),
            'port':8040,'cards':'1,2,3,8','command':command,'log':str(log),'time':time.time(),
            'template_service':template,'free_gpu_check':free,'generation_configuration_changed':False,
            'environment':{k:env[k] for k in INFRA_KEYS+('CUDA_VISIBLE_DEVICES','TMPDIR','XDG_CACHE_HOME') if k in env}}
        atomic_json(output/'launch.json',value)
        print(json.dumps({k:value[k] for k in ('pid','identity','port','cards','log')}),flush=True)
        return value


def stop(service, receipt):
    if receipt.exists():
        return True
    if not prior.base.legacy.alive(service):
        atomic_json(receipt,{'already_stopped':True,'time':time.time(),'service':service})
        return True
    owned(service)
    health = endpoint(service['port'])
    if not health['healthy'] or not health['idle']:
        return False
    owned(service)
    os.kill(service['pid'],signal.SIGTERM)
    atomic_json(receipt,{'time':time.time(),'service':service,'signal_sent':'SIGTERM'})
    return True


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source-deployment',type=Path,required=True)
    a = p.parse_args()
    launch(a.output,a.source_deployment)
