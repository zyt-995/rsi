"""Trusted in-container entrypoint, invoked with -I, never imported from /app."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

PREFIX = Path('/opt/rsi-offline-v1')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_command(args):
    # -P excludes the working directory, but retains installed/user packages.
    # Unlike -I this preserves agent-installed Cython extensions and packages.
    args = list(args)
    if len(args) > 1 and Path(args[0]).name.startswith('python') and args[1] in ('-c', '-m'):
        args.insert(1, '-P')
    return args


def main(task, log):
    log = Path(log)
    if log.parent != Path('/logs/rsi-grading-attempts') or not log.name.isalnum():
        raise ValueError('unregistered_grading_log_directory')
    log.mkdir(parents=True, exist_ok=False)
    original = Path('/proc/1/environ').read_bytes().decode().split('\0')
    env = dict(x.split('=', 1) for x in original if '=' in x)
    env = {k: v for k, v in env.items()
           if not any(s in k.lower() for s in ('proxy', 'token', 'key', 'secret'))
           and not k.startswith(('PYTHON', 'PYTEST'))}
    env.update(PATH=env.get('PATH', '/usr/local/bin:/usr/bin:/bin'), HOME='/root',
               PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', UV_OFFLINE='1',
               UV_PYTHON_DOWNLOADS='never', NO_PROXY='*', no_proxy='*', PYTHONSAFEPATH='1')
    os.environ.clear()
    os.environ.update(env)
    os.chdir('/app')  # Preserve task-relative paths; isolate imports, not the task.
    isolation = load('trusted_network_isolation', PREFIX / 'isolated.py')
    isolation.Path = lambda p: log / 'network.json' if p == '/logs/verifier/network.json' else Path(p)
    isolation.isolate()
    grade = load('trusted_original_grader', PREFIX / 'grade.py')
    grade.LOG = log
    grade.subprocess = SimpleNamespace(
        run=lambda args, **kw: subprocess.run(safe_command(args), **kw),
        check_output=lambda args, **kw: subprocess.check_output(safe_command(args), **kw))
    (log / 'import-isolation.json').write_text(json.dumps({
        'trusted_entry_isolated': sys.flags.isolated == 1,
        'python_safe_path': True, 'working_directory': '/app',
        'official_assertions_changed': False}))
    try:
        grade.grade(task)
    except Exception as exc:
        (log / 'offline-error.json').write_text(json.dumps({
            'error': type(exc).__name__ + ':' + str(exc), 'is_algorithmic_zero': False}))
        raise


if __name__ == '__main__':
    main(*sys.argv[1:])
