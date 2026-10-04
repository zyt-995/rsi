"""Launch only the currently free GPUs 5-8 with unchanged TP4 inference argv."""
import argparse
import csv
import importlib.util
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resume.services import TEMPLATE, PROJECT
from self_skill_rsi_dual import service as prior
from . import storage

PORTS, GROUPS = [8041], [[5, 6, 7, 8]]
CARDS = '5,6,7,8'


def free_cards():
    rows = csv.reader(io.StringIO(subprocess.check_output(['nvidia-smi',
        '--query-gpu=index,uuid,memory.used', '--format=csv,noheader,nounits'], text=True)))
    inventory = {int(r[0]): (r[1].strip(), int(r[2])) for r in rows}
    if any(inventory[i][1] > 128 for i in GROUPS[0]):
        raise RuntimeError('selected_cards_no_longer_free')
    used = csv.reader(io.StringIO(subprocess.check_output(['nvidia-smi',
        '--query-compute-apps=gpu_uuid,pid', '--format=csv,noheader,nounits'], text=True)))
    if any(r and r[0].strip() in {inventory[i][0] for i in GROUPS[0]} for r in used):
        raise RuntimeError('selected_cards_have_compute_process')


def owned(value):
    if not prior.prior.base.legacy.alive(value):
        raise RuntimeError('service_not_alive')
    proc = Path('/proc') / str(value['pid'])
    argv = proc.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
    env = dict(s.split('=', 1) for s in proc.joinpath('environ').read_bytes().decode().split('\0') if '=' in s)
    if (proc.stat().st_uid != os.getuid() or value['port'] != 8041 or value['cards'] != CARDS
            or env.get('CUDA_VISIBLE_DEVICES') != CARDS or argv != value['command']):
        raise RuntimeError('not_exact_registered_owned_service')


def launch(output):
    if output.exists():
        raise RuntimeError('new_inference_directory_required')
    free_cards()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 8041))
    template = read_json(TEMPLATE)
    command = list(template['command'])
    command[command.index('--port') + 1] = '8041'
    if command[command.index('--tensor-parallel-size') + 1] != '4':
        raise RuntimeError('unexpected_tp_configuration')
    output.mkdir(parents=True)
    env = os.environ.copy()
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(k, None)
    env.update({k: v for k, v in template['environment'].items() if k in prior.INFRA_KEYS})
    env.update(storage.environment(), CUDA_VISIBLE_DEVICES=CARDS)
    log = output / 'service-8041.log'
    with log.open('xb') as stream:
        child = subprocess.Popen(command, cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    value = {'pid': child.pid, 'identity': prior.prior.base.core.process_identity(child.pid),
             'port': 8041, 'cards': CARDS, 'command': command, 'log': str(log), 'time': time.time(),
             'template': str(TEMPLATE), 'template_sha256': file_hash(TEMPLATE)}
    atomic_json(output / 'launch-8041.json', value)
    atomic_json(output / 'launches.json', {'services': [value], 'time': time.time()})


def accept(output):
    value = read_json(output / 'launch-8041.json')
    deadline = time.monotonic() + 900
    while not prior.endpoint(8041)['healthy']:
        owned(value)
        if time.monotonic() > deadline:
            raise RuntimeError('service_startup_timeout')
        time.sleep(5)
    owned(value)
    spec = importlib.util.spec_from_file_location('storage_inference_check', PROJECT / 'scripts/check-rsi-inference.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    atomic_json(output / 'smoke-8041.json', checker.check(8041))
    paths = [TEMPLATE, output / 'launch-8041.json', output / 'smoke-8041.json',
             PROJECT / 'scripts/check-rsi-inference.py',
             Path('/data3/ytzhang/models/Qwen3.8-27B/generation_config.json')]
    atomic_json(output / 'deployment.json', {'time': time.time(), 'ports': PORTS, 'gpu_groups': GROUPS,
        'model': 'Qwen/Qwen3.8-27B', 'services': [value], 'functional_checks_passed': True,
        'generation_configuration_changed': False, 'candidate_validation_parallelism': 1,
        'evidence_hashes': {str(p): file_hash(p) for p in paths}})


def stop(value, receipt):
    if receipt.exists():
        return True
    if not prior.prior.base.legacy.alive(value):
        atomic_json(receipt, {'already_stopped': True, 'time': time.time(), 'service': value})
        return True
    owned(value)
    health = prior.endpoint(value['port'])
    if not health['healthy'] or not health['idle']:
        return False
    owned(value)
    os.kill(value['pid'], signal.SIGTERM)
    atomic_json(receipt, {'time': time.time(), 'service': value, 'signal_sent': 'SIGTERM'})
    return True


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--launch', action='store_true'); p.add_argument('--accept', action='store_true')
    a = p.parse_args()
    if a.launch: launch(a.output.resolve())
    if a.accept: accept(a.output.resolve())
