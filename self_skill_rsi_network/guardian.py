"""Existing bounded supervisor, pointed at the network-audited recovery."""
import argparse
from pathlib import Path
import subprocess
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_resilient import guardian as shared
from self_skill_rsi_resume.guardian import release
from . import campaign as c


def launch(root, env, serial):
    command = [str(shared.PYTHON), '-m', 'self_skill_rsi_network.campaign', '--root', str(root), '--run', '--allow-native-runs']
    with (root / f'controller-{serial:02d}.log').open('ab', buffering=0) as log:
        child = subprocess.Popen(command, cwd=shared.PROJECT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    value = {'pid': child.pid, 'identity': c.base.core.process_identity(child.pid), 'started': time.time(),
             'command': command, 'serial': serial}
    atomic_json(root / f'controller-launches/{serial:02d}.json', value)
    atomic_json(root / 'supervisor_launch.json', value)
    return child, value


def run(root):
    shared.c, shared.launch, shared.release = c, launch, release
    shared.TERMINAL = shared.TERMINAL | {'awaiting_environment'}
    original = shared.runtime.signal_owned_process
    def owned(record, module, sig):
        if module == 'self_skill_rsi_resilient.campaign': module = 'self_skill_rsi_network.campaign'
        return original(record, module, sig)
    shared.runtime.signal_owned_process = owned
    shared.run(root)


def install(root):
    m = c.verify(root)
    if read_json(root / 'status.json')['state'] != 'prepared_not_running':
        raise RuntimeError('initial_launch_requires_prepared_state')
    unit = 'rsi-self-skill-network-' + root.name
    command = ['systemd-run', '--user', '--unit', unit, '--property=Restart=on-failure',
        '--property=RestartSec=10', '--property=StartLimitIntervalSec=0', '--property=KillMode=process',
        '--property=WorkingDirectory=' + str(shared.PROJECT), str(shared.PYTHON),
        '-m', 'self_skill_rsi_network.guardian', '--root', str(root), '--run']
    subprocess.run(command, check=True, timeout=20)
    atomic_json(root / 'guardian-service.json', {'unit': unit + '.service', 'command': command, 'time': time.time()})
    atomic_json(shared.PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json', {'root': str(root), 'protocol': m['schema'],
        'previous_root': str(c.SOURCE), 'updated': time.time(), 'campaign_module': 'self_skill_rsi_network.campaign',
        'inference_ports': m['ports'], 'gpu_groups': c.prior.GROUPS, 'guardian_unit': unit + '.service',
        'offline_verifier_manifest': m['resilience_migration']['grader']})


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', type=Path, required=True)
    p.add_argument('--install', action='store_true'); p.add_argument('--run', action='store_true'); a = p.parse_args()
    if a.install: install(a.root.resolve())
    elif a.run: run(a.root.resolve())
    else: p.error('explicit action required')
