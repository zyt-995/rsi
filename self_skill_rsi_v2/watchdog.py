"""Read-only v2 health checks; no implicit trial restart or Codex wake-up."""
import argparse
import os
from pathlib import Path
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from . import campaign as c


def check(root):
    m = c.verify(root)
    now = time.time()
    status = read_json(root / 'status.json', {})
    heartbeat = read_json(root / 'heartbeat.json', {})
    alive = c.legacy.alive(read_json(root / 'supervisor_launch.json', {}))
    value = {'time': now, 'pid': os.getpid(), 'identity': c.core.process_identity(os.getpid()),
             'controller_alive': alive, 'status': status, 'metrics': c.report(root, m),
             'heartbeat_age': now - heartbeat.get('time', 0), 'automatic_retry': False, 'codex_wakeup': False}
    if status.get('state') in ('complete', 'blocked', 'paused'):
        value['terminal'] = status['state']
    elif not alive:
        value['alert'] = 'controller_not_alive; no_automatic_trial_retry'
    elif value['heartbeat_age'] > 120:
        value['alert'] = 'controller_heartbeat_stale'
    else:
        value['disk'] = c.core.docker_disk_status(c.core.PROJECT)
        if not value['disk']['ok']:
            value['alert'] = 'disk_admission_low'
    return value


def run(root):
    with c.legacy.exclusive(root / 'watchdog.lock'):
        while True:
            try:
                value = check(root)
            except Exception as exc:
                value = {'time': time.time(), 'alert': f'{type(exc).__name__}:{exc}', 'automatic_retry': False}
            atomic_json(root / 'watchdog/status.json', value)
            snapshot = root / 'monitor' / (str(int(time.time() // 1800)) + '.json')
            if not snapshot.exists():
                atomic_json(snapshot, value)
            if value.get('alert'):
                atomic_json(root / 'alerts/latest.json', value)
            if value.get('terminal') or not value.get('controller_alive', False):
                return
            time.sleep(20)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    run(parser.parse_args().root)
