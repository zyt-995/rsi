"""Read-only health monitor. NEVER retries trials or starts another controller.

Writes a 30-minute snapshot; this is not an automatic Codex wake-up. An interrupted
model request or solve requires artifact inspection, not blind resampling.
"""
import argparse
import os
import time
from pathlib import Path

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from . import campaign as c


def check(root):
    m = c.verify(root)
    status = read_json(root / 'status.json', {})
    launch = read_json(root / 'supervisor_launch.json', {})
    heartbeat = read_json(root / 'heartbeat.json', {})
    alive = c.legacy.alive(launch)
    now = time.time()
    report = {'time': now, 'pid': os.getpid(), 'identity': c.core.process_identity(os.getpid()),
              'controller_alive': alive, 'status': status,
              'heartbeat_age': now - heartbeat.get('time', 0), 'metrics': c.report(root, m),
              'automatic_retry': False, 'codex_wakeup': False}
    if status.get('state') in ('complete', 'blocked', 'paused'):
        report['terminal'] = status['state']
    elif not alive:
        report['alert'] = 'controller_not_alive; no_automatic_trial_or_request_retry'
    elif report['heartbeat_age'] > 120:
        report['alert'] = 'controller_heartbeat_stale'
    else:
        disk = c.core.docker_disk_status(c.core.PROJECT)
        report['disk'] = disk
        if not disk['ok']:
            report['alert'] = 'disk_admission_low; new_native_trials_will_fail_closed'
    return report


def run(root):
    with c.legacy.exclusive(root / 'watchdog.lock'):
        while True:
            try:
                value = check(root)
            except Exception as exc:
                value = {'time': time.time(), 'alert': f'{type(exc).__name__}:{exc}', 'automatic_retry': False}
            atomic_json(root / 'watchdog/status.json', value)
            p = root / 'monitor' / (str(int(time.time() // 1800)) + '.json')
            if not p.exists():
                atomic_json(p, value)
            if value.get('alert'):
                atomic_json(root / 'alerts/latest.json', value)
            if value.get('terminal') or not value.get('controller_alive', False):
                return
            time.sleep(20)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    run(parser.parse_args().root)
