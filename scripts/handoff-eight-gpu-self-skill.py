#!/usr/bin/env python3
"""One-shot checked handoff. No trial cancellation, score retry or auto recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_accel import campaign as c

AUDIT = Path('/data2/ytzhang/rsi-harness-runs/inference-acceleration-20260930')
PYTHON = str(PROJECT / '.venv-terminal/bin/python')
spec = importlib.util.spec_from_file_location('service_control', PROJECT / 'scripts/rsi-inference-service-control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def status(state, **extra):
    value = {'time': time.time(), 'state': state, 'pid': os.getpid(),
             'identity': c.base.core.process_identity(os.getpid()), 'automatic_retry': False, **extra}
    atomic_json(AUDIT / 'handoff-status.json', value)
    print(json.dumps(value), flush=True)


def wait_boundary(source):
    status('waiting_for_full_task_boundary', source=str(source))
    start = time.monotonic()
    last_report = start
    # One legal step can include a long native solve, three learning calls and
    # two parallel validations. This is a handoff wait, not a new task budget.
    while time.monotonic() - start < 28800:
        value = read_json(source / 'status.json', {})
        if value.get('state') in ('blocked', 'complete'):
            raise RuntimeError('source_not_migratable:' + str(value))
        if value.get('state') == 'paused':
            try:
                c.v2.check_source_stopped(source)
            except RuntimeError:
                time.sleep(5)
                continue
            return
        if not c.base.legacy.alive(read_json(source / 'supervisor_launch.json', {})):
            raise RuntimeError('source_died_before_clean_boundary')
        if time.monotonic() - last_report >= 30:
            status('waiting_for_full_task_boundary', source=str(source),
                   current_uid=value.get('uid'), completed=value.get('completed'),
                   waiting_seconds=time.monotonic() - start)
            last_report = time.monotonic()
        time.sleep(10)
    raise RuntimeError('boundary_wait_timeout; native_trial_retained')


def ready(service):
    started = time.monotonic()
    while time.monotonic() - started < 420:
        if not c.base.legacy.alive(service):
            raise RuntimeError('new_service_exited:' + service['log'])
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{service["port"]}/health', timeout=3) as r:
                if r.status == 200:
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(5)
    raise RuntimeError('service_startup_timeout:' + service['log'])


def smoke(port):
    out = AUDIT / f'final-{port}-functional.json'
    subprocess.run([PYTHON, str(PROJECT / 'scripts/check-rsi-inference.py'), '--port', str(port), '--output', str(out)],
                   cwd=PROJECT, check=True, timeout=180)
    return out


def bench(port):
    out = AUDIT / f'final-{port}-speed.json'
    subprocess.run([PYTHON, str(PROJECT / 'scripts/benchmark-rsi-inference.py'), '--port', str(port),
                    '--output', str(out), '--label', f'final-tp4-{port}-concurrent', '--repeats', '1'],
                   cwd=PROJECT, check=True, timeout=240)
    return out


def execute(source, target, resume_stopped=False):
    if target.exists():
        raise RuntimeError('target_exists; inspect_before_any_rerun')
    baseline = read_json(AUDIT / 'tp2-eager.json')
    faster = read_json(AUDIT / 'tp4-decode-graph.json')
    if faster['median_decode_tokens_per_second'] <= baseline['median_decode_tokens_per_second'] * 1.05:
        raise RuntimeError('no_speedup')
    if not read_json(AUDIT / 'shadow-functional.json')['passed']:
        raise RuntimeError('shadow_functional_checks_failed')
    if not (source / 'PAUSE').exists():
        raise RuntimeError('source_boundary_pause_not_requested')
    if resume_stopped:
        previous = read_json(AUDIT / 'handoff-status.json')
        if (previous.get('state') != 'handoff_blocked' or 'Address already in use' not in previous.get('error', '')
                or c.base.legacy.alive(previous)):
            raise RuntimeError('not_the_reviewed_port_preflight_failure')
        c.v2.check_source_stopped(source)
        atomic_json(AUDIT / ('port-preflight-recovery-' + str(int(time.time())) + '.json'),
                    {'previous': previous, 'reason': 'closed HTTP TIME_WAIT is not an active listener',
                     'time': time.time(), 'native_trial_retries': 0})
    else:
        wait_boundary(source)
    old = c.offline.verify(source)
    completed = c.boundary(source, old)
    status('boundary_verified', preserved_official_uids=[e['uid'] for e,_ in completed])
    targets = [(8040, '0,1', 2696163, '346210649'),
               (8041, '2,3', 1638443, '343904856'),
               (8050, '4,5,6,7', 3534252, '417136439')]
    for port, cards, pid, identity in targets:
        if resume_stopped:
            if c.base.core.process_identity(pid) == identity:
                raise RuntimeError('reviewed_old_service_is_still_live')
            control.available_port(port)
        else:
            control.stop(SimpleNamespace(port=port, cards=cards, pid=pid, identity=identity))
    for _ in range(30):
        raw = subprocess.check_output(['nvidia-smi', '--query-gpu=index,memory.used', '--format=csv,noheader,nounits'], text=True)
        memory = {int(row.split(',')[0]): int(row.split(',')[1]) for row in raw.splitlines()}
        if all(memory[i] < 1024 for i in range(8)):
            break
        time.sleep(3)
    else:
        raise RuntimeError('GPU_memory_not_released; no_force_kill')
    services = [control.launch(SimpleNamespace(port=port, cards=cards, mode='graph'))
                for port,cards in [(8040,'0,1,2,3'), (8041,'4,5,6,7')]]
    status('starting_two_tp4_services', services=services)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(ready, services))
        checks = list(pool.map(smoke, [8040,8041]))
        speed_checks = list(pool.map(bench, [8040,8041]))
    speedup = min(read_json(p)['median_decode_tokens_per_second'] for p in speed_checks) / baseline['median_decode_tokens_per_second']
    if speedup <= 1.05:
        raise RuntimeError('final_concurrent_deployment_did_not_speed_up')
    files = checks + speed_checks + [AUDIT / name for name in ('tp2-eager.json', 'tp4-decode-graph.json', 'shadow-functional.json')]
    files += [PROJECT / 'scripts' / name for name in ('run-qwen38-accelerated-service.sh', 'benchmark-rsi-inference.py',
               'check-rsi-inference.py', 'rsi-inference-service-control.py', 'handoff-eight-gpu-self-skill.py', 'launch-accelerated-self-skill.py')]
    deployed = {'ports': [8040,8041], 'gpu_groups': [[0,1,2,3],[4,5,6,7]], 'model': old['model'],
                'functional_checks_passed': True, 'median_decode_speedup': speedup,
                'services': services, 'time': time.time(), 'evidence_hashes': {str(p): c.sha(p) for p in files},
                'dtype': 'bfloat16', 'max_model_len': 262144, 'max_num_seqs': 1,
                'tensor_parallel_size': 4, 'cudagraph_mode': 'FULL_DECODE_ONLY',
                'formal_task_replays': 0, 'algorithm_changed': False}
    deployment = AUDIT / 'deployment.json'
    if deployment.exists():
        raise RuntimeError('deployment_already_published')
    atomic_json(deployment, deployed)
    c.prepare(target, source, deployment)
    status('prepared', target=str(target), speedup=speedup)
    subprocess.run([PYTHON, str(PROJECT / 'scripts/launch-accelerated-self-skill.py'), '--root', str(target), '--allow-native-runs'],
                   cwd=PROJECT, check=True, timeout=60)
    status('accelerated_campaign_running', target=str(target), speedup=speedup,
           preserved_official_uids=[e['uid'] for e,_ in completed])


def launch(source, target):
    if (AUDIT / 'handoff_launch.json').exists():
        raise RuntimeError('handoff_launch_record_exists; inspect_before_any_restart')
    command = [PYTHON, str(Path(__file__).resolve()), '--source', str(source), '--target', str(target), '--execute']
    log = AUDIT / ('handoff-' + str(int(time.time())) + '.log')
    with log.open('xb') as out:
        child = subprocess.Popen(command, cwd=PROJECT, stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
    value = {'pid': child.pid, 'identity': c.base.core.process_identity(child.pid), 'time': time.time(),
             'command': command, 'log': str(log), 'automatic_retry': False}
    atomic_json(AUDIT / 'handoff_launch.json', value)
    print(json.dumps(value), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--target', type=Path, required=True)
    actions = p.add_mutually_exclusive_group(required=True)
    actions.add_argument('--execute', action='store_true')
    actions.add_argument('--launch', action='store_true')
    p.add_argument('--resume-stopped-services', action='store_true',
                   help='Explicit recovery only after the audited TIME_WAIT preflight failure; no task retries.')
    args = p.parse_args()
    if args.launch:
        if args.resume_stopped_services:
            p.error('reviewed recovery requires --execute, not --launch')
        launch(args.source.resolve(), args.target.resolve())
        sys.exit(0)
    try:
        with c.base.legacy.exclusive(AUDIT / 'handoff.lock'):
            execute(args.source.resolve(), args.target.resolve(), args.resume_stopped_services)
    except Exception as exc:
        status('handoff_blocked', error=f'{type(exc).__name__}:{exc}')
        raise
