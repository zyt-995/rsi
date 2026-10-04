"""Independent finite-restart supervisor for the six registered probe solves."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_resilient import guardian as existing, runtime
from . import campaign as c

PROJECT = c.PROJECT
PYTHON = PROJECT / '.venv-terminal/bin/python'


def launch(root, env, serial):
    command = [str(PYTHON), '-m', c.MODULE, '--root', str(root), '--run', '--allow-native-runs']
    with (root / f'controller-{serial:02d}.log').open('ab', buffering=0) as log:
        child = subprocess.Popen(command, cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    record = {'pid': child.pid, 'identity': c.base.core.process_identity(child.pid),
              'started': time.time(), 'command': command, 'serial': serial}
    atomic_json(root / f'controller-launches/{serial:02d}.json', record)
    atomic_json(root / 'supervisor_launch.json', record)
    return child


def tick(root, m, env, child):
    if child is not None:
        child.poll()
    state = read_json(root / 'guardian-state.json', {'launches': 0, 'last_launch': 0})
    status = read_json(root / 'status.json', {})
    control = read_json(root / 'supervisor_launch.json', {})
    alive = c.base.legacy.alive(control)
    now = time.time()
    health = {str(p): existing.endpoint(p) for p in m['ports']}
    action = existing.action(status, alive, state['launches'], now, state['last_launch'])
    if action == 'launch' and all(x['healthy'] for x in health.values()):
        state.pop('endpoint_unavailable_since', None)
        state['launches'] += 1
        state['last_launch'] = now
        atomic_json(root / 'guardian-state.json', state)
        child = launch(root, env, state['launches'])
        alive = True
    elif action == 'launch':
        action = 'endpoint_unavailable'
        state.setdefault('endpoint_unavailable_since', now)
        if now - state['endpoint_unavailable_since'] >= 300:
            status = {'state': 'needs_attention', 'time': now, 'reason': 'inference_unavailable_300s'}
            atomic_json(root / 'status.json', status)
    elif action == 'exhausted':
        status = {'state': 'needs_attention', 'time': now, 'reason': 'controller_restart_budget_exhausted'}
        atomic_json(root / 'status.json', status)
    for p in root.glob('stream/**/child.json'):
        native = read_json(p)
        if c.base.legacy.alive(native) and now - read_json(p.parent / 'launch.json', {}).get('started', now) > 10920:
            receipt = root / 'expired-children' / f"{native['pid']}-{native['identity']}.json"
            old = read_json(receipt)
            sig = signal.SIGKILL if old and now - old['time'] > 60 else signal.SIGTERM
            runtime.signal_owned_process(native, str(c.base.core.HARBOR), sig)
            if not old:
                atomic_json(receipt, {'time': now, 'process': native})
    heartbeat = read_json(root / 'heartbeat.json', {})
    age = now - heartbeat.get('time', now)
    if alive and age > 300 and not existing.live_children(root) and all(h['idle'] for h in health.values()):
        receipt = root / 'stalled-controllers' / f"{control['pid']}-{control['identity']}.json"
        old = read_json(receipt)
        sig = signal.SIGKILL if old and now - old['time'] > 60 else signal.SIGTERM
        runtime.signal_owned_process(control, c.MODULE, sig)
        if not old:
            atomic_json(receipt, {'time': now, 'process': control})
    value = {'time': now, 'pid': os.getpid(), 'controller_alive': alive,
             'controller_launches': state['launches'], 'action': action, 'status': status,
             'endpoints': health, 'heartbeat_age': age, 'codex_wakeup': False,
             'automatic_model_replay': False}
    if status.get('state') == 'needs_attention' or (alive and age > 180):
        value['alert'] = status.get('reason', 'heartbeat_stale')
        atomic_json(root / 'alerts/latest.json', value)
    if status.get('state') in existing.TERMINAL and not alive and now - status.get('time', now) >= 300:
        value['resource_release_requested'] = existing.release(root, m, env)
    atomic_json(root / 'guardian-state.json', state)
    atomic_json(root / 'watchdog/status.json', value)
    snap = root / 'monitor' / f'{int(now // 1800)}.json'
    if not snap.exists():
        atomic_json(snap, {**value, 'metrics': c.report(root, m)})
    return child, value


def run(root):
    with c.base.legacy.exclusive(root / 'guardian.lock'):
        m = c.verify(root)
        env = runner_environment(m['ports'][0])
        os.environ.update(env)
        atomic_json(root / 'watchdog_launch.json', {'pid': os.getpid(),
            'identity': c.base.core.process_identity(os.getpid()), 'started': time.time()})
        child = None
        while True:
            try:
                c.verify(root)
                child, value = tick(root, m, env, child)
                print(json.dumps(value), flush=True)
            except Exception as exc:
                value = {'time': time.time(), 'reason': type(exc).__name__ + ':' + str(exc)}
                atomic_json(root / 'alerts/guardian-error.json', value)
                print(json.dumps(value), flush=True)
            time.sleep(20)


def install(root):
    m = c.verify(root)
    if read_json(root / 'status.json').get('state') != 'prepared_not_running':
        raise RuntimeError('initial_launch_requires_prepared_state')
    unit = 'rsi-self-skill-probe-' + root.name
    cmd = ['systemd-run', '--user', '--unit', unit, '--property=Restart=on-failure',
           '--property=RestartSec=10', '--property=StartLimitIntervalSec=0', '--property=KillMode=process',
           '--property=WorkingDirectory=' + str(PROJECT), str(PYTHON), '-m',
           'self_skill_rsi_probe.guardian', '--root', str(root), '--run']
    subprocess.run(cmd, check=True, timeout=20)
    atomic_json(root / 'guardian-service.json', {'unit': unit + '.service', 'command': cmd, 'time': time.time()})
    atomic_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json', {'root': str(root),
        'protocol': m['schema'], 'previous_root': m['source_root'], 'updated': time.time(),
        'campaign_module': c.MODULE, 'inference_ports': m['ports'],
        'gpu_groups': [[0, 1, 2, 3], [4, 5, 6, 7]], 'guardian_unit': unit + '.service'})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--run', action='store_true')
    p.add_argument('--install', action='store_true')
    a = p.parse_args()
    if a.run:
        run(a.root.resolve())
    elif a.install:
        install(a.root.resolve())
    else:
        p.error('action required')
