"""Start, inspect or pause a prepared five-method session. Start performs paid model calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from _bootstrap import REPO
from rsi_portable.paths import WORKSPACE, RUNTIME


def roots_for(session):
    from swe_chain_evo.common import read
    m = read(session / 'manifest.json')
    return [Path(m['reuse_controls'][0]['root'])] + [Path(r['root']) for r in m['runs']]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['start', 'status', 'pause'])
    p.add_argument('--session', type=Path, required=True)
    args = p.parse_args(); session = args.session.resolve()
    from swe_chain_evo.common import read, atomic, sha
    roots = roots_for(session)
    if args.action == 'status':
        result = []
        for root in roots:
            result.append({'root': str(root), 'metrics': read(root / 'metrics.json') if (root / 'metrics.json').exists() else None})
        print(json.dumps(result, indent=2)); return
    if args.action == 'pause':
        (session / 'HOLD').write_text('Requested pause; current work is retained.\n')
        for root in roots:
            (root / 'PAUSE').write_text('Requested pause at task/update boundary.\n')
        return
    from within_chain.contracts import validate, check_pins, budget_snapshot
    from formal_swe_chain.campaign import validate as validate_control
    if not read(session / 'readiness.json')['passed']:
        raise RuntimeError('Complete the zero-model environment preparation first.')
    for i, root in enumerate(roots):
        cfg = read(root / 'configuration.json')
        validate_control(root, cfg) if i == 0 else validate(root)
        check_pins(root)
        state = read(root / 'native-campaign.json') if (root / 'native-campaign.json').exists() else {}
        for arm, item in state.get('arms', {}).items():
            if item.get('status') in ('needs_review', 'budget_exhausted'):
                raise RuntimeError(f'{arm}: inspect saved interruption and reconcile billing before resuming; never discard paid attempts.')
        # Locks, rather than recycled process IDs, determine whether a runner is live.
        import fcntl
        with (root / '.campaign.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cfg = read(roots[0] / 'configuration.json')
    snapshot = budget_snapshot(cfg)
    if snapshot['available_usd'] <= 0:
        raise RuntimeError('Configured session budget is exhausted.')
    key = os.environ.get(cfg['model']['credential_env'], '').strip()
    if not key:
        raise RuntimeError('Set ' + cfg['model']['credential_env'] + ' in the shell before start; never put credentials in Git.')
    path = Path(cfg['credential_file']); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(key)
    atomic(session / 'RUN_AUTHORIZATION.json', {'authorized': True,
        'user_instruction': 'Explicit scripts/run.py start invocation', 'session_root': str(session),
        'configuration_hashes': {str(r): sha(r / 'configuration.json') for r in roots},
        'budget_before_launch': snapshot, 'time': time.time()})
    (session / 'HOLD').unlink(missing_ok=True)
    env = dict(os.environ, PYTHONPATH=str(WORKSPACE) + os.pathsep + str(RUNTIME),
               PYTHONDONTWRITEBYTECODE='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    workers = []
    for i, root in enumerate(roots):
        (root / 'PAUSE').unlink(missing_ok=True)
        module = 'formal_swe_chain.campaign' if i == 0 else 'within_chain.campaign'
        with (root / 'launcher.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, '-u', '-m', module, '--root', str(root)],
                cwd=WORKSPACE, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        workers.append({'root': str(root), 'pid': proc.pid, 'module': module})
    atomic(session / 'workers.json', workers)
    print(json.dumps({'started': workers, 'budget': snapshot}, indent=2))


if __name__ == '__main__':
    main()
