"""Bounded dispatcher restart and durable alerts, never a model-task retry loop."""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import campaign as c
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json


def restart_allowed(history, now):
    recent = [t for t in history if now - t < 3600]
    return len(recent) < 3 and (not recent or now - max(recent) >= 60)


def owned_dispatcher(root, launch):
    if not launch or not c.legacy.alive(launch):
        return False
    pid = launch['pid']
    proc = Path(f'/proc/{pid}')
    args = (proc / 'cmdline').read_bytes().decode().split('\0')[:-1]
    expected = ['-u', '-m', 'cross_chain_rsi.campaign', '--root', str(root), '--run']
    if proc.stat().st_uid != os.getuid() or args[1:] != expected:
        raise RuntimeError('live dispatcher identity/command mismatch; no signals or restart')
    return True


def start(root):
    c.verify(root)
    # Never launch around a held controller lock or an unexpected dispatcher.
    with c.legacy.exclusive(root / 'scheduler.lock'), c.legacy.exclusive(c.SOURCE_ROOT / 'scheduler.lock'):
        if owned_dispatcher(root, read_json(root / 'supervisor_launch.json')):
            raise RuntimeError('dispatcher already live')
    with (root / 'supervisor.log').open('ab', buffering=0) as stream:
        child = subprocess.Popen([sys.executable, '-u', '-m', 'cross_chain_rsi.campaign', '--root', str(root), '--run'],
                                 cwd=c.PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    launch = {'pid': child.pid, 'identity': c.core.process_identity(child.pid), 'started': time.time(), 'root': str(root)}
    atomic_json(root / 'supervisor_launch.json', launch)
    atomic_json(root / 'watchdog/launches' / f'{time.time_ns()}.json', launch)
    return child


def run(root):
    with c.legacy.exclusive(root / 'watchdog.lock'):
        history = read_json(root / 'watchdog/restarts.json', {}).get('times', [])
        stopping, child = False, None
        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            now = time.time()
            if child:
                child.poll()
            value = {'time': now, 'pid': os.getpid(), 'restarts_last_hour': sum(now - t < 3600 for t in history)}
            try:
                c.verify(root)
                launch = read_json(root / 'supervisor_launch.json')
                alive = owned_dispatcher(root, launch)
                status = read_json(root / 'status.json', {})
                value['supervisor_alive'] = alive
                if stopping or (root / 'PAUSE').exists():
                    if stopping and alive:
                        os.kill(launch['pid'], signal.SIGTERM)  # drain; task workers are not killed
                    value['terminal'] = 'paused'
                elif not alive and status.get('terminal') in ('complete', 'paused'):
                    value['terminal'] = status['terminal']
                elif not alive:
                    if restart_allowed(history, now):
                        child = start(root)
                        history.append(now)
                        atomic_json(root / 'watchdog/restarts.json', {'times': history})
                        value['action'] = 'dispatcher_started_or_restarted; saved worker identities retained'
                    else:
                        value['alert'] = 'dispatcher restart cooldown/limit reached; no model retries'
                elif now - status.get('time', now) > 180:
                    value['alert'] = 'dispatcher heartbeat stale; do not kill or create a duplicate'
                elif status.get('terminal') == 'needs_review':
                    value['alert'] = 'dispatcher alive, unresolved jobs quarantined'
            except Exception as exc:
                value['alert'] = f'{type(exc).__name__}:{exc}'
            atomic_json(root / 'watchdog/status.json', value)
            if value.get('alert'):
                atomic_json(root / 'alerts/watchdog.json', value)
            snapshot = root / 'watchdog/monitor' / f'{int(now // 1800)}.json'
            if not snapshot.exists():
                atomic_json(snapshot, value)
            if value.get('terminal'):
                return
            time.sleep(20)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    a = p.parse_args()
    run(a.root.resolve())
