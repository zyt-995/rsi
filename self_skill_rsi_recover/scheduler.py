"""Two shared TP4 lanes, lagging-arm priority, durable non-replaying job records."""
import argparse
import os
from pathlib import Path
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_resilient.guardian import endpoint
from self_skill_rsi_dual.service import stop as stop_service
from self_skill_rsi_storage import storage
from . import campaign as c

PROJECT = c.base.core.PROJECT
PYTHON = PROJECT / '.venv-terminal/bin/python'


def candidates(root, m, occupied_arms):
    ranks = {'self-skill': 0, 'baseline': 1, 'skill-rsi': 2}
    pending = []
    for arm in c.ARMS:
        if arm in occupied_arms: continue
        e = c.next_entry(root, m, arm)
        if e and c.acceptance(root, e['chain']): pending.append((e, arm))
    return sorted(pending, key=lambda pair: (pair[0]['uid'], ranks[pair[1]]))


def jobs(root):
    active, errors = [], []
    for path in sorted((root / 'jobs').glob('*/*/launch.json')):
        record = read_json(path); result = read_json(path.parent / 'result.json')
        if c.base.legacy.alive(record): active.append(record)
        elif result is None:
            errors.append({'arm': record['arm'], 'uid': record['uid'], 'reason': 'worker_exited_without_result;no_automatic_replay'})
        elif result['state'] != 'complete': errors.append(result)
    return active, errors


def launch(root, entry, arm, port, env):
    folder = root / 'jobs' / arm / entry['uid']; folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'launch.json').exists(): raise RuntimeError('job_already_launched_no_blind_retry')
    args = [str(PYTHON), '-m', 'self_skill_rsi_recover.worker', '--root', str(root), '--arm', arm,
            '--uid', entry['uid'], '--port', str(port), '--output', str(folder / 'result.json')]
    # A durable intent without a PID is a review boundary, never a new sample.
    if (folder / 'intent.json').exists(): raise RuntimeError('interrupted_job_launch_requires_review')
    atomic_json(folder / 'intent.json', {'arm': arm, 'uid': entry['uid'], 'port': port, 'time': time.time()})
    with (folder / 'worker.log').open('ab', buffering=0) as log:
        child = subprocess.Popen(args, cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    value = {'pid': child.pid, 'identity': c.base.core.process_identity(child.pid), 'started': time.time(),
             'arm': arm, 'uid': entry['uid'], 'task': entry['task'], 'port': port, 'command': args}
    atomic_json(folder / 'launch.json', value)
    return child


def run(root):
    storage.activate(); os.environ.update(runner_environment(8041))
    env = os.environ.copy()
    with c.base.legacy.exclusive(root / 'scheduler.lock'):
        m = c.verify(root)
        identity = {'pid': os.getpid(), 'identity': c.base.core.process_identity(os.getpid()), 'time': time.time()}
        atomic_json(root / 'supervisor_launch.json', identity)
        atomic_json(root / 'watchdog_launch.json', identity)
        children = []; terminal_since = None
        while True:
            for child in children: child.poll()
            c.verify(root)
            active, errors = jobs(root)
            states = {p: endpoint(p) for p in c.PORTS}
            choices = candidates(root, m, {x['arm'] for x in active})
            if not errors and not (root / 'PAUSE').exists():
                for port in c.PORTS:
                    if port in {x['port'] for x in active} or not choices: continue
                    if not states[port]['healthy'] or not states[port]['idle']: continue
                    entry, arm = choices.pop(0)
                    children.append(launch(root, entry, arm, port, env))
                    active, errors = jobs(root)
            complete = all(c.next_entry(root, m, a) is None for a in c.ARMS)
            waiting = {a: (c.next_entry(root, m, a) or {}).get('task') for a in c.ARMS}
            if complete: state = 'complete'
            elif errors: state = 'needs_attention'
            elif (root / 'PAUSE').exists(): state = 'pausing' if active else 'paused'
            elif active: state = 'running'
            else: state = 'awaiting_environment' if not choices else 'awaiting_inference'
            metrics = c.report(root, m)
            reason = errors[0]['reason'] if errors else None
            if state == 'awaiting_environment':
                reason = 'offline environment not ready: ' + ', '.join(str(x) for x in waiting.values())
            status = {'state': state, 'time': time.time(), **identity, 'active': active, 'next_tasks': waiting,
                'task': ', '.join(x['arm'] + ':' + x['task'] for x in active),
                'completed': metrics['completed_update_steps'], 'common_scored': metrics['historical_matched_comparison']['n'],
                'reason': reason, 'automatic_model_replay': False}
            status['time'] = time.time()
            atomic_json(root / 'status.json', status); atomic_json(root / 'metrics.json', metrics)
            atomic_json(root / 'heartbeat.json', {**identity, 'time': time.time()})
            watch = {'time': time.time(), 'controller_alive': True, 'status': status,
                     'endpoints': states, 'action': state, 'alert': status['reason']}
            atomic_json(root / 'watchdog/status.json', watch)
            if reason: atomic_json(root / 'alerts/latest.json', watch)
            if state in ('complete', 'needs_attention', 'paused') and not active:
                terminal_since = terminal_since or time.time()
                if time.time() - terminal_since > 300:
                    deployed = read_json(Path(m['inference_migration']['deployment']))
                    if all(stop_service(s, root / 'released-services' / f"{s['port']}.json") for s in deployed['services']):
                        atomic_json(root / 'resource-release.json', {'time': time.time(), 'state': state, 'owned_services_released': True})
                        return
            else: terminal_since = None
            time.sleep(10)


def install(root):
    m = c.verify(root)
    if any(c.base.legacy.alive(read_json(p)) for p in (root / 'jobs').glob('*/*/launch.json')): raise RuntimeError('active_workers_at_install')
    unit = 'rsi-self-skill-recover-' + root.name
    args = ['systemd-run', '--user', '--unit', unit, '--property=Restart=on-failure', '--property=RestartSec=20',
            '--property=StartLimitIntervalSec=300', '--property=StartLimitBurst=3', '--property=KillMode=process',
            '--property=WorkingDirectory=' + str(PROJECT), str(PYTHON), '-m', 'self_skill_rsi_recover.scheduler',
            '--root', str(root), '--run']
    subprocess.run(args, check=True, timeout=20)
    atomic_json(root / 'guardian-service.json', {'unit': unit + '.service', 'command': args, 'time': time.time()})
    atomic_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json', {'root': str(root), 'protocol': 'three-arm-continuation-v1',
        'previous_root': str(c.previous.SOURCE), 'updated': time.time(), 'campaign_module': 'self_skill_rsi_recover.campaign',
        'inference_ports': c.PORTS, 'gpu_groups': c.GROUPS, 'guardian_unit': unit + '.service',
        'offline_verifier_manifest': m['resilience_migration']['grader']})


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path)
    p.add_argument('--run', action='store_true'); p.add_argument('--install', action='store_true')
    a = p.parse_args()
    if a.install: install(a.root.resolve())
    elif a.run: run(a.root.resolve())
    else: p.error('explicit action required')
