#!/usr/bin/env python3
"""Explicitly launch one prepared offline campaign and its read-only monitor."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_offline import campaign as c


def launch(root):
    root = root.resolve()
    m = c.verify(root)
    if read_json(root / 'status.json', {}).get('state') != 'prepared_not_running':
        raise RuntimeError('launch_requires_prepared_not_running; never_automatically_retry')
    for name in ('supervisor_launch.json', 'watchdog_launch.json'):
        if (root / name).exists():
            raise RuntimeError('launch_record_already_exists; inspect_before_any_restart')
    runtime_manifest = Path(m['offline_migration']['manifest'])
    env = c.base.core.runner_environment(m['ports'][0])
    python = str(PROJECT / '.venv-terminal/bin/python')
    for role, module in [('supervisor', 'campaign'), ('watchdog', 'watchdog')]:
        command = [python, '-m', 'self_skill_rsi_offline.' + module, '--root', str(root)]
        if role == 'supervisor':
            command += ['--run', '--allow-native-runs']
        with (root / (role + '.log')).open('ab', buffering=0) as log:
            child = subprocess.Popen(command, cwd=PROJECT, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        atomic_json(root / (role + '_launch.json'), {'pid': child.pid,
            'identity': c.base.core.process_identity(child.pid), 'started': time.time(),
            'command': command, 'protocol': m['schema'], 'offline_manifest': str(runtime_manifest)})
        if role == 'supervisor':
            for _ in range(20):
                if child.poll() is not None:
                    raise RuntimeError('controller_exited_at_startup; inspect_supervisor_log')
                if read_json(root / 'status.json', {}).get('state') == 'running':
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('controller_did_not_enter_running_state')
    atomic_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json', {
        'root': str(root), 'protocol': m['schema'], 'previous_root': m['offline_migration']['source_root'],
        'updated': time.time(), 'offline_verifier_manifest': str(runtime_manifest),
        'campaign_module': 'self_skill_rsi_offline.campaign'})
    atomic_json(PROJECT / 'runs/ACTIVE_OFFLINE_VERIFIER.json', {
        'manifest': str(runtime_manifest), 'manifest_sha256': m['offline_migration']['manifest_sha256'],
        'registered_tasks': len(m['stream']), 'acceptance': m['offline_migration']['acceptance']})
    print({'root': str(root), 'controller': read_json(root / 'supervisor_launch.json')['pid'],
           'watchdog': read_json(root / 'watchdog_launch.json')['pid'],
           'preserved_official_scores': len(m['offline_migration']['preserved_official_uids'])})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--allow-native-runs', action='store_true', required=True)
    args = p.parse_args()
    launch(args.root)
