"""Run original assertions with cached, task-specific dependency bootstrap."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path('/opt/rsi-offline-v1')
LOG = Path('/logs/verifier')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def grade(task):
    payload = json.loads((ROOT / 'payload.json').read_text()); cfg = payload['tasks'][task]
    if cfg['profile'] != 'extension-script':
        old = load('unchanged_previous_grader', ROOT / 'original_grade.py')
        old.LOG, old.subprocess = LOG, subprocess
        return old.grade(task)
    for name, expected in cfg['tests'].items():
        if hashlib.sha256((Path('/tests') / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('official_test_changed:' + name)
    plan = load('extension_bootstrap', ROOT / 'extension_bootstrap.py').translate(Path('/tests/test.sh').read_text())
    if plan != cfg['bootstrap']: raise RuntimeError('bootstrap_plan_changed')
    missing = []
    for package in plan['apt_packages']:
        result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True)
        if result.returncode or result.stdout != 'install ok installed': missing.append(package)
    if missing:
        debs = sorted((ROOT / 'debs' / task).glob('*.deb'))
        if not debs: raise RuntimeError('missing_offline_system_dependencies:' + str(missing))
        subprocess.run(['dpkg', '-i', *map(str, debs)], check=True, timeout=180,
            env={**os.environ, 'LD_PRELOAD': str(ROOT / 'singleuid-compat.so'), 'DEBIAN_FRONTEND': 'noninteractive'})
    if plan['runtime'] == 'standalone':
        interpreter = ROOT / ('python' if plan['python_minor'] == '3.13' else 'python312') / 'bin/python3'
        venv = ROOT / 'venvs' / task
        subprocess.run([str(ROOT / 'uv'), 'venv', '--offline', '--python', str(interpreter), str(venv)], check=True, timeout=60)
        python = str(venv / 'bin/python')
    else:
        python = shutil.which(plan['executable'])
        if not python: raise RuntimeError('official_python_missing:' + plan['executable'])
    if plan['requirements']:
        subprocess.run([str(ROOT / 'uv'), 'pip', 'install', '--offline', '--no-index', '--break-system-packages',
            '--find-links', str(ROOT / 'wheels'), '--python', python, *plan['requirements']], check=True, timeout=180)
    subprocess.run([python, '-P', '-c', 'import pytest; import importlib.metadata as m; m.version("pytest-json-ctrf")'], check=True, timeout=30)
    os.environ.update(RSI_PYTEST_PYTHON=python, RSI_PYTEST_WRAPPER=str(ROOT / 'pytest_entry.py'),
                      RSI_GRADING_LOG=str(LOG), PYTHONSAFEPATH='1')
    if plan['runtime'] == 'standalone': os.environ['PATH'] = str(Path(python).parent) + ':' + os.environ['PATH']
    if plan['ocaml_fixture']:
        repo = ROOT / 'resources/ocaml.git'
        tag = payload['extra_resources']['ocaml_tag']
        commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', tag + '^{commit}'], text=True).strip()
        if commit != payload['extra_resources']['ocaml_commit']: raise RuntimeError('ocaml_fixture_changed')
        os.environ.update(GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='url.' + repo.as_uri() + '.insteadOf',
            GIT_CONFIG_VALUE_0=payload['extra_resources']['ocaml_url'], GIT_CONFIG_KEY_1='protocol.file.allow', GIT_CONFIG_VALUE_1='always')
    script = plan['script'].replace('/logs/verifier', str(LOG))
    (LOG / 'offline-test.sh').write_text(script)
    shell = subprocess.run(['/bin/bash', str(LOG / 'offline-test.sh')], timeout=540)
    exits = [json.loads(x) for x in (LOG / 'pytest-exits.jsonl').read_text().splitlines()]
    if len(exits) != plan['pytest_calls'] or any(e['exit_code'] not in (0, 1) for e in exits):
        raise RuntimeError('pytest_infrastructure_exit:' + str(exits))
    expected = int(all(e['exit_code'] == 0 for e in exits))
    if (LOG / 'reward.txt').read_text().strip() != str(expected): raise RuntimeError('official_reward_disagreement')
    report = json.loads((LOG / 'ctrf.json').read_text())
    if plan['pytest_calls'] == 2:
        shutil.copy2(LOG / 'ctrf.json', LOG / 'additional-ctrf.json')
        original = json.loads((LOG / 'original-repo-ctrf.json').read_text())
        report['results']['tests'] += original['results']['tests']
        for k in ('tests', 'passed', 'failed', 'skipped', 'pending', 'other'):
            report['results']['summary'][k] = report['results']['summary'].get(k, 0) + original['results']['summary'].get(k, 0)
        (LOG / 'ctrf.json').write_text(json.dumps(report))
    summary, tests = report['results']['summary'], report['results']['tests']
    if not tests or len(tests) != summary['tests'] or any(t['status'] not in ('passed', 'failed') for t in tests):
        raise RuntimeError('incomplete_official_tests')
    if expected != int(summary['failed'] == 0): raise RuntimeError('pytest_report_disagreement')
    (LOG / 'offline-result.json').write_text(json.dumps({'status': 'complete', 'task': task, 'profile': cfg['profile'],
        'reward': expected, 'summary': summary, 'official_test_hashes': cfg['tests'], 'python': python,
        'official_workflow_preserved': True, 'bootstrap_replacements': plan['bootstrap_replacements'],
        'shell_exit': shell.returncode, 'pytest_exits': exits}))
