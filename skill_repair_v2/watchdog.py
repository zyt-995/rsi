"""Persistent bounded controller recovery, never retries a solve or model call."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import campaign as c
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json


def run(root):
    with c.legacy.exclusive(root / 'watchdog.lock'):
        history = read_json(root / 'watchdog/restarts.json', {}).get('times', [])
        child = None
        while True:
            if child:
                child.poll()
            now = time.time()
            value = {'time': now, 'pid': os.getpid(), 'identity': c.core.process_identity(os.getpid())}
            try:
                c.verify(root)
                status = read_json(root / 'status.json', {})
                launch = read_json(root / 'supervisor_launch.json', {})
                alive = bool(launch) and c.legacy.alive(launch)
                value['supervisor_alive'] = alive
                if (root / 'PAUSE').exists() or read_json(root / 'handoff_resumed.json'):
                    value['terminal'] = 'paused' if (root / 'PAUSE').exists() else 'finished'
                elif not alive:
                    if c.watchdog.restart_allowed(history, now):
                        with c.legacy.exclusive(root / 'scheduler.lock'):
                            pass
                        with (root / 'supervisor.log').open('ab', buffering=0) as stream:
                            child = subprocess.Popen([sys.executable, '-u', '-m', 'skill_repair_v2.campaign',
                                '--root', str(root), '--run'], cwd=c.core.PROJECT,
                                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                        atomic_json(root / 'supervisor_launch.json', {'pid': child.pid,
                            'identity': c.core.process_identity(child.pid), 'started': now})
                        history.append(now)
                        atomic_json(root / 'watchdog/restarts.json', {'times': history})
                        value['action'] = 'controller_started; existing workers adopted'
                    else:
                        value['alert'] = 'controller_restart_limit; manual_review_required'
                elif now - status.get('time', now) > 180:
                    value['alert'] = 'controller_heartbeat_stale; no_duplicate_launched'
                elif status.get('disk', {}).get('ok') is False:
                    value['alert'] = 'disk_admission_paused; existing_tasks_continue'
            except Exception as exc:
                value['alert'] = f'{type(exc).__name__}:{exc}'
            atomic_json(root / 'watchdog/status.json', value)
            if value.get('alert'):
                atomic_json(root / 'alerts/latest.json', value)
            if value.get('terminal'):
                return
            time.sleep(20)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    run(p.parse_args().root)
