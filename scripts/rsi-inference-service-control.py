#!/usr/bin/env python3
"""Auditable, exact-PID service operations for the eight-GPU handoff only."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

PROJECT = Path('/home/ytzhang/rsi-harness')
AUDIT = Path('/data2/ytzhang/rsi-harness-runs/inference-acceleration-20260930')
ALLOWED = {(8040, '0,1'), (8041, '2,3'), (8042, '4,5'), (8044, '6,7'),
           (8050, '4,5,6,7'), (8040, '0,1,2,3'), (8041, '4,5,6,7')}


def process(pid):
    p = Path('/proc') / str(pid)
    if p.stat().st_uid != os.getuid():
        raise RuntimeError('not_our_process')
    argv = p.joinpath('cmdline').read_bytes().decode().split('\0')[:-1]
    env = dict(x.split('=', 1) for x in p.joinpath('environ').read_bytes().decode().split('\0') if '=' in x)
    identity = p.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19]
    return argv, env, identity


def save(name, value):
    AUDIT.mkdir(parents=True, exist_ok=True)
    path = AUDIT / name
    with path.open('x') as out:
        json.dump(value, out, indent=2)
    return path


def idle(port):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=10) as r:
        lines = r.read().decode().splitlines()
    found = {}
    for key in ('vllm:num_requests_running', 'vllm:num_requests_waiting'):
        values = [float(x.rsplit(' ', 1)[1]) for x in lines if x.startswith(key + '{') or x.startswith(key + ' ')]
        if not values or any(v != 0 for v in values):
            raise RuntimeError('service_not_idle:' + key)
        found[key] = values
    return found


def stop(a):
    argv, env, identity = process(a.pid)
    if (a.port, a.cards) not in ALLOWED or identity != a.identity:
        raise RuntimeError('unregistered_target_or_pid_reuse')
    if 'vllm.entrypoints.openai.api_server' not in argv or argv[argv.index('--port') + 1] != str(a.port):
        raise RuntimeError('not_the_registered_api_server')
    if env.get('CUDA_VISIBLE_DEVICES') != a.cards:
        raise RuntimeError('wrong_gpu_set')
    metrics = idle(a.port)
    selected = {k:v for k,v in env.items() if k in ('CUDA_VISIBLE_DEVICES', 'CUDA_HOME', 'NO_PROXY', 'no_proxy',
                'HF_HUB_OFFLINE', 'HF_DATASETS_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY',
                'DO_NOT_TRACK', 'VLLM_NO_USAGE_STATS', 'VLLM_USE_FLASHINFER_SAMPLER')}
    save(f'stop-{a.port}-{a.pid}-{int(time.time())}.json', {'pid': a.pid, 'identity': identity,
         'argv': argv, 'environment': selected, 'idle_metrics': metrics, 'time': time.time(),
         'reason': 'user-authorized eight-GPU inference acceleration; exact idle service only'})
    if process(a.pid)[2] != identity:
        raise RuntimeError('process_changed')
    os.kill(a.pid, signal.SIGTERM)
    print(json.dumps({'signal': 'SIGTERM', 'pid': a.pid, 'port': a.port}), flush=True)


def available_port(port):
    # A closed HTTP server can leave TIME_WAIT connections. SO_REUSEADDR
    # permits a legitimate restart, but does not share an active listener
    # (SO_REUSEPORT is deliberately not enabled).
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', port))


def launch(a):
    if (a.port, a.cards) not in {(8050, '4,5,6,7'), (8040, '0,1,2,3'), (8041, '4,5,6,7')}:
        raise RuntimeError('unregistered_launch')
    available_port(a.port)
    AUDIT.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    log = AUDIT / f'service-{a.port}-{a.mode}-{stamp}.log'
    cmd = ['bash', str(PROJECT / 'scripts/run-qwen38-accelerated-service.sh'), a.cards, str(a.port), '4', a.mode]
    with log.open('xb') as out:
        child = subprocess.Popen(cmd, cwd=PROJECT, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True, stdin=subprocess.DEVNULL)
    value = {'pid': child.pid, 'identity': process(child.pid)[2], 'port': a.port, 'cards': a.cards,
             'mode': a.mode, 'command': cmd, 'log': str(log), 'time': time.time()}
    save(f'launch-{a.port}-{stamp}.json', value)
    print(json.dumps(value), flush=True)
    return value


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['stop', 'launch'])
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--cards', required=True)
    p.add_argument('--pid', type=int)
    p.add_argument('--identity')
    p.add_argument('--mode', choices=['eager', 'graph'], default='graph')
    a = p.parse_args()
    if a.action == 'stop' and (not a.pid or not a.identity):
        p.error('stop requires exact --pid and --identity')
    (stop if a.action == 'stop' else launch)(a)
