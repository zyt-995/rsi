#!/usr/bin/env python3
"""Explicit launch of an audited task-boundary inference migration."""
import argparse
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_accel import campaign as c
from self_skill_rsi_accel.environment import runner_environment


def launch(root):
    root = root.resolve()
    m = c.verify(root)
    if read_json(root / 'status.json', {}).get('state') != 'prepared_not_running':
        raise RuntimeError('launch_requires_prepared_not_running')
    for name in ('supervisor_launch.json', 'watchdog_launch.json'):
        if (root / name).exists():
            raise RuntimeError('launch_record_exists; no_automatic_retry')
    with c.offline.runtime.inference_lease(m['ports']):
        pass
    env = runner_environment(m['ports'][0])
    for role, module in [('supervisor', 'campaign'), ('watchdog', 'watchdog')]:
        command = [str(PROJECT / '.venv-terminal/bin/python'), '-m', 'self_skill_rsi_accel.' + module,
                   '--root', str(root)]
        if role == 'supervisor':
            command += ['--run', '--allow-native-runs']
        with (root / (role + '.log')).open('ab', buffering=0) as log:
            child = subprocess.Popen(command, cwd=PROJECT, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        atomic_json(root / (role + '_launch.json'), {'pid': child.pid,
            'identity': c.base.core.process_identity(child.pid), 'started': time.time(), 'command': command})
        if role == 'supervisor':
            for _ in range(40):
                if child.poll() is not None:
                    raise RuntimeError('controller_exited; inspect_supervisor_log')
                if read_json(root / 'status.json', {}).get('state') == 'running':
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('controller_startup_timeout')
    atomic_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json', {
        'root': str(root), 'protocol': m['schema'], 'previous_root': m['inference_migration']['source_root'],
        'updated': time.time(), 'offline_verifier_manifest': m['offline_migration']['manifest'],
        'campaign_module': 'self_skill_rsi_accel.campaign', 'inference_ports': m['ports'],
        'gpu_groups': [[0,1,2,3], [4,5,6,7]]})
    print({'root': str(root), 'controller': read_json(root / 'supervisor_launch.json')['pid'],
           'watchdog': read_json(root / 'watchdog_launch.json')['pid'],
           'preserved_official_scores': len(m['inference_migration']['preserved_official_uids'])})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--allow-native-runs', action='store_true', required=True)
    launch(p.parse_args().root)
