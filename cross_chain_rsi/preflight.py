"""Reuse unchanged environments; separately allow enough time for the grader.

Only infrastructure probes, never model solves. The source campaign keeps its
original 600-second verifier; all three NEW target arms get 1800 seconds.
"""
import argparse
import concurrent.futures
import importlib.util
import json
import subprocess
import time
from pathlib import Path

from . import campaign as c
from rsi_harness.terminal_infra import atomic_json, fingerprint, readiness
from rsi_harness.clean_rsi import read_json


def helpers():
    spec = importlib.util.spec_from_file_location('existing_preflight', c.PROJECT / 'scripts/preflight-terminal-environments.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(root, name):
    old = helpers()
    with c.legacy.exclusive(root / '_infra' / f'{name}.lock'):
        prior = readiness(root, name)
        if prior.get('status') == 'ready':
            return prior
        if prior.get('status') == 'running':
            raise RuntimeError('existing probe still running:' + name)
        task = c.DATASET / name
        if not old.inputs_available(task):
            raise RuntimeError('cached base/image missing:' + name)
        output = root / '_infra/trials' / name / str(time.time_ns())
        output.mkdir(parents=True)
        atomic_json(output / 'previous_ticket.json', prior)
        record = {'task': name, 'fingerprint': fingerprint(task), 'status': 'running', 'started': time.time(),
                  'output': str(output), 'verifier_timeout': c.VERIFIER_TIMEOUT, 'model_calls': 0}
        command = [str(c.core.HARBOR), 'trials', 'start', '--path', str(task), '--trials-dir', str(output),
                   '--agent-import-path', 'rsi_harness.terminal_smoke_agent:TerminalSmokeAgent',
                   '--environment-build-timeout-multiplier', '4', '--verifier-timeout', str(c.VERIFIER_TIMEOUT)]
        template = c.core.harbor_command(task, output, 'baseline', output / 'unused-state')
        for index, value in enumerate(template[:-1]):
            if value == '--ve':
                command += [value, template[index + 1]]
        target = root / '_infra/readiness' / f'{name}.json'
        atomic_json(output / 'launch.json', {'command': command, 'formal': False, 'reason': 'grader budget, not algorithm retry'})
        try:
            with (output / 'preflight.log').open('ab', buffering=0) as stream:
                process = subprocess.Popen(command, cwd=c.PROJECT, env=c.core.runner_environment(8040),
                                           stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                record.update(pid=process.pid, identity=c.core.process_identity(process.pid))
                atomic_json(target, record)
                process.wait(timeout=5400)
            paths = list(output.glob('*/result.json'))
            if len(paths) != 1:
                raise RuntimeError('missing/ambiguous preflight result')
            evidence = old.test_report(paths[0].parent)
            image = evidence['runtime']['image']
            image_id = c.image_id(image)
            if not image_id:
                raise RuntimeError('image disappeared')
            record.update(status='ready', image=image, image_id=image_id, **evidence)
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}:{exc}')
        record['finished'] = time.time()
        atomic_json(target, record)
        print(json.dumps({k: v for k, v in record.items() if k != 'error'}), flush=True)
        return record


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--task', action='append', required=True)
    p.add_argument('--jobs', type=int, default=2)
    args = p.parse_args()
    with c.legacy.exclusive(args.root / '_infra/preflight.lock'):
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(lambda task: check(args.root, task), args.task))
    return 0 if all(r['status'] == 'ready' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
