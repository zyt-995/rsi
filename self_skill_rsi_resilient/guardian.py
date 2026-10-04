"""Independent systemd-supervised guardian; bounded restart, persistent alerts."""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_accel.environment import runner_environment
from . import campaign as c, runtime

PROJECT = c.base.core.PROJECT
PYTHON = PROJECT / '.venv-terminal/bin/python'
TERMINAL = {'complete', 'paused', 'needs_attention'}


def live_children(root):
    return [read_json(p) for p in root.glob('stream/**/child.json')
            if c.base.legacy.alive(read_json(p))]


def action(status, alive, restarts, now, last_launch):
    if status.get('state') in TERMINAL:
        return 'terminal'
    if alive:
        return 'monitor'
    if restarts >= c.POLICY['controller_crash_restarts'] + 1:
        return 'exhausted'
    delay = 0 if restarts == 0 else c.POLICY['controller_backoff_seconds'][restarts - 1]
    return 'launch' if now - last_launch >= delay else 'backoff'


def launch(root, env, serial):
    command = [str(PYTHON), '-m', 'self_skill_rsi_resilient.campaign',
               '--root', str(root), '--run', '--allow-native-runs']
    with (root / f'controller-{serial:02d}.log').open('ab', buffering=0) as log:
        child = subprocess.Popen(command, cwd=PROJECT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    value = {'pid': child.pid, 'identity': c.base.core.process_identity(child.pid),
             'started': time.time(), 'command': command, 'serial': serial}
    atomic_json(root / f'controller-launches/{serial:02d}.json', value)
    atomic_json(root / 'supervisor_launch.json', value)
    return child, value


def endpoint(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=3) as r:
            lines = r.read().decode().splitlines()
        gauges = {}
        for key in ('vllm:num_requests_running', 'vllm:num_requests_waiting'):
            values = [float(x.rsplit(' ', 1)[1]) for x in lines if x.startswith((key + '{', key + ' '))]
            if not values:
                raise ValueError('missing_request_gauge')
            gauges[key] = sum(values)
        return {'healthy': True, 'idle': not any(gauges.values()), 'gauges': gauges}
    except Exception as exc:
        return {'healthy': False, 'idle': False, 'reason': type(exc).__name__}


def release(root, m, env):
    """Exact recorded API processes only; never GPU-index based killing."""
    if live_children(root) or c.base.legacy.alive(read_json(root / 'supervisor_launch.json', {})):
        return False
    deployment = read_json(Path(m['inference_migration']['deployment']))
    for service in deployment['services']:
        receipt = root / 'released-services' / f"{service['port']}.json"
        if receipt.exists():
            continue
        if not c.base.legacy.alive(service):
            atomic_json(receipt, {'already_stopped': True, 'service': service, 'time': time.time()})
            continue
        state = endpoint(service['port'])
        if not state['healthy'] or not state['idle']:
            return False
        command = [str(PYTHON), str(PROJECT / 'scripts/rsi-inference-service-control.py'), 'stop',
                   '--port', str(service['port']), '--cards', service['cards'],
                   '--pid', str(service['pid']), '--identity', service['identity']]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            atomic_json(root / 'alerts/release-failed.json', {'time': time.time(), 'port': service['port'],
                        'reason': result.stderr[-2000:]})
            return False
        atomic_json(receipt, {'time': time.time(), 'service': service, 'signal_sent': 'SIGTERM'})
    return True


def tick(root, m, env, child=None):
    if child is not None:
        child.poll()  # Reap our child; /proc identity also rejects zombies.
    control = read_json(root / 'supervisor_launch.json', {})
    status = read_json(root / 'status.json', {})
    state = read_json(root / 'guardian-state.json', {'launches': 0, 'last_launch': 0})
    alive = c.base.legacy.alive(control)
    now = time.time()
    # A native child past the pre-existing three-hour watchdog is not entitled
    # to hold GPUs indefinitely. Signal only its recorded PID, never a GPU group.
    for p in root.glob('stream/**/child.json'):
        native = read_json(p)
        started = read_json(p.parent / 'launch.json', {}).get('started', now)
        if now - started > 10920 and c.base.legacy.alive(native):
            receipt = root / 'expired-children' / f"{native['pid']}-{native['identity']}.json"
            previous = read_json(receipt)
            sig = signal.SIGKILL if previous and now - previous['time'] > 60 else signal.SIGTERM
            runtime.signal_owned_process(native, str(c.base.core.HARBOR), sig)
            if not previous:
                atomic_json(receipt, {'time': now, 'process': native, 'signal': 'SIGTERM'})
    choice = action(status, alive, state['launches'], now, state['last_launch'])
    health = {str(p): endpoint(p) for p in m['ports']}
    if choice == 'launch' and not all(x['healthy'] for x in health.values()):
        state.setdefault('endpoint_unavailable_since', now)
        choice = 'endpoint_unavailable'
        if now - state['endpoint_unavailable_since'] >= 300:
            status = {'state': 'needs_attention', 'time': now, 'reason': 'inference_endpoint_unavailable_300s'}
            atomic_json(root / 'status.json', status)
    elif choice == 'launch':
        state.pop('endpoint_unavailable_since', None)
        state['launches'] += 1
        state['last_launch'] = now
        atomic_json(root / 'guardian-state.json', state)  # Charge budget before Popen.
        child, control = launch(root, env, state['launches'])
        alive = True
    elif choice == 'exhausted':
        status = {'state': 'needs_attention', 'time': now, 'reason': 'bounded_controller_restarts_exhausted'}
        atomic_json(root / 'status.json', status)
    heartbeat = read_json(root / 'heartbeat.json', {})
    value = {'time': now, 'pid': os.getpid(), 'identity': c.base.core.process_identity(os.getpid()),
        'controller_alive': alive, 'controller_launches': state['launches'], 'status': status,
        'action': choice, 'endpoints': health, 'heartbeat_age': now - heartbeat.get('time', now),
        'automatic_model_replay': False, 'codex_wakeup': False,
        'guardian_independent_of_controller': True}
    if status.get('state') == 'needs_attention' or (alive and value['heartbeat_age'] > 180):
        value['alert'] = status.get('reason', 'controller_heartbeat_stale')
        atomic_json(root / 'alerts/latest.json', value)
    if alive and value['heartbeat_age'] > 300 and not live_children(root) and all(x['idle'] for x in health.values()):
        receipt = root / 'stalled-controllers' / f"{control['pid']}-{control['identity']}.json"
        previous = read_json(receipt)
        sig = signal.SIGKILL if previous and now - previous['time'] > 60 else signal.SIGTERM
        runtime.signal_owned_process(control, 'self_skill_rsi_resilient.campaign', sig)
        if not previous:
            atomic_json(receipt, {'time': now, 'process': control, 'reason': 'stale_heartbeat_and_idle_endpoints'})
    if status.get('state') in TERMINAL and not alive and not live_children(root):
        if now - status.get('time', now) >= c.POLICY['idle_release_seconds']:
            value['resource_release_requested'] = release(root, m, env)
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
            'identity': c.base.core.process_identity(os.getpid()), 'time': time.time(), 'systemd_supervised': True})
        child = None
        while True:
            try:
                # Controller checks full seals before every stage; independent monitor
                # verifies too so it can refuse restart after code/artifact corruption.
                c.verify(root)
                child, value = tick(root, m, env, child)
                print(json.dumps({k: value[k] for k in ('time', 'action', 'controller_alive', 'status')}, ensure_ascii=False), flush=True)
            except Exception as exc:
                value = {'time': time.time(), 'reason': type(exc).__name__ + ':' + str(exc)}
                atomic_json(root / 'alerts/guardian-error.json', value)
                print(json.dumps(value), flush=True)
                # Keep monitoring, but do not launch unverified work.
            time.sleep(c.POLICY['guardian_poll_seconds'])


def install(root):
    m = c.verify(root)
    if read_json(root / 'status.json')['state'] != 'prepared_not_running':
        raise RuntimeError('initial_launch_requires_prepared_state')
    unit = 'rsi-self-skill-resilient-' + root.name.split('-')[-1]
    command = ['systemd-run', '--user', '--unit', unit, '--property=Restart=on-failure',
        '--property=RestartSec=10', '--property=StartLimitIntervalSec=0',
        '--property=KillMode=process', '--property=WorkingDirectory=' + str(PROJECT),
        str(PYTHON), '-m', 'self_skill_rsi_resilient.guardian', '--root', str(root), '--run']
    subprocess.run(command, check=True, timeout=20)
    atomic_json(root / 'guardian-service.json', {'unit': unit + '.service', 'command': command, 'time': time.time()})
    atomic_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json', {
        'root': str(root), 'protocol': m['schema'], 'previous_root': m['resilience_migration']['source_root'],
        'updated': time.time(), 'offline_verifier_manifest': m['resilience_migration']['grader'],
        'campaign_module': 'self_skill_rsi_resilient.campaign', 'inference_ports': m['ports'],
        'gpu_groups': [[0,1,2,3],[4,5,6,7]], 'guardian_unit': unit + '.service'})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--run', action='store_true')
    p.add_argument('--install', action='store_true')
    a = p.parse_args()
    if a.install:
        install(a.root.resolve())
    elif a.run:
        run(a.root.resolve())
    else:
        p.error('action required')
