"""Report missing environment requirements; never call a paid model or download images."""
import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from _bootstrap import REPO
from rsi_portable.paths import WORKSPACE, docker_env


def doctor(docker=False):
    checks = []
    def add(name, passed, detail):
        checks.append({'name': name, 'passed': bool(passed), 'detail': detail})
    add('python', sys.version_info[:2] == (3, 12), sys.version.split()[0])
    for name in ('harbor', 'litellm', 'tokenizers', 'tiktoken', 'claude_agent_sdk', 'pytest'):
        try:
            m = importlib.import_module(name)
            add(name, True, str(Path(m.__file__).resolve()))
        except Exception as exc:
            add(name, False, type(exc).__name__ + ': ' + str(exc))
    for name in ('git', 'docker', 'bwrap', 'claude', 'go', 'uv'):
        path = (os.environ.get('RSI_' + name.upper() + '_BINARY') if name in ('docker', 'go') else None) or shutil.which(name)
        available = bool(path and Path(path).is_file() and os.access(path, os.X_OK))
        add('binary:' + name, available, path or 'Install this dependency; see docs/reproduction.md')
        if available and name in ('go', 'claude'):
            proc = subprocess.run([path, 'version' if name == 'go' else '--version'], capture_output=True, text=True, timeout=20)
            expected = 'go1.25.0' if name == 'go' else '2.1.299'
            add('version:' + name, proc.returncode == 0 and expected in proc.stdout, proc.stdout.strip()[:150])
        if available and name == 'bwrap':
            proc = subprocess.run([path, '--ro-bind', '/', '/', '--unshare-user', '/bin/true'], capture_output=True, timeout=10)
            add('sandbox_user_namespace', proc.returncode == 0, proc.stderr.decode(errors='replace')[-300:] or 'available')
    manifest = json.loads((REPO / 'upstream/manifest.json').read_text())
    for item in manifest['repositories']:
        proc = subprocess.run(['git', '-C', str(REPO / item['path']), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        add('upstream:' + item['name'], proc.returncode == 0 and proc.stdout.strip() == item['commit'], proc.stdout.strip())
    add('workspace', (WORKSPACE / 'runtime-project/.venv-terminal/bin/python').exists(), str(WORKSPACE))
    storage = WORKSPACE if WORKSPACE.exists() else REPO
    add('free_space', shutil.disk_usage(storage).free > 10 * 1024**3,
        f'{shutil.disk_usage(storage).free / 1024**3:.1f} GiB; run prepare.py --plan for selected image archive sizes')
    if docker:
        try:
            proc = subprocess.run(['docker', 'info', '--format', '{{json .}}'], env=docker_env(), capture_output=True, text=True, timeout=30)
            info = json.loads(proc.stdout) if proc.returncode == 0 else {}
            add('docker_daemon', proc.returncode == 0, {k: info.get(k) for k in ('ServerVersion', 'DockerRootDir', 'Architecture')} if info else 'Docker info failed')
            compose = subprocess.run(['docker', 'compose', 'version', '--short'], env=docker_env(), capture_output=True, text=True, timeout=15)
            add('docker_compose', compose.returncode == 0, compose.stdout.strip())
        except Exception as exc:
            add('docker_daemon', False, str(exc))
    report = {'passed': all(c['passed'] for c in checks), 'checks': checks, 'paid_model_calls': 0,
        'api_key_present': bool(os.environ.get('OPENROUTER_API_KEY')), 'api_key_required_for_environment_setup': False}
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--docker', action='store_true')
    raise SystemExit(0 if doctor(p.parse_args().docker)['passed'] else 1)
