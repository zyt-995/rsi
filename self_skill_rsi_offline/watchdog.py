"""Read-only monitoring with offline code/artifact seals; no automatic retries."""
import argparse
import os
from pathlib import Path
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from . import campaign as c


def check(root):
    m = c.verify(root)
    status = read_json(root / 'status.json', {})
    heartbeat = read_json(root / 'heartbeat.json', {})
    alive = c.base.legacy.alive(read_json(root / 'supervisor_launch.json', {}))
    value = {'time': time.time(), 'pid': os.getpid(), 'identity': c.base.core.process_identity(os.getpid()),
             'controller_alive': alive, 'status': status, 'metrics': c.v2.report(root, m),
             'heartbeat_age': time.time() - heartbeat.get('time', 0),
             'automatic_retry': False, 'codex_wakeup': False,
             'offline_manifest': m['offline_migration']['manifest']}
    if status.get('state') in ('complete', 'blocked', 'paused'):
        value['terminal'] = status['state']
        if status['state'] == 'blocked':
            value['alert'] = status.get('reason', 'campaign_blocked')
    elif not alive:
        value['alert'] = 'controller_not_alive; no_automatic_retry'
    elif value['heartbeat_age'] > 120:
        value['alert'] = 'controller_heartbeat_stale'
    else:
        value['disk'] = c.base.core.docker_disk_status(c.base.core.PROJECT)
        if not value['disk']['ok']:
            value['alert'] = 'disk_admission_low'
    return value


def run(root):
    with c.base.legacy.exclusive(root / 'watchdog.lock'):
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
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    run(p.parse_args().root)
