"""Offline execution of the official shell workflow; legacy tasks use legacy code."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT = Path('/opt/rsi-offline-v1')
LOG = Path('/logs/verifier')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    print('offline bootstrap:', shlex.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, timeout=180)


def grade(task):
    payload = json.loads((ROOT / 'payload.json').read_text())
    cfg = payload['tasks'][task]
    if cfg['profile'] != 'official-script':
        old = load('frozen_legacy_grader', ROOT / 'legacy_grade.py')
        old.LOG, old.subprocess = LOG, subprocess
        return old.grade(task)
    for name, expected in cfg['tests'].items():
        if hashlib.sha256((Path('/tests') / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('official_test_changed:' + name)
    translator = load('audited_bootstrap_translation', ROOT / 'bootstrap.py')
    plan = translator.translate(Path('/tests/test.sh').read_text())
    if plan != cfg['bootstrap']:
        raise RuntimeError('bootstrap_plan_changed')
    # Packages from the original shell bootstrap, installed after solving only.
    packages = plan['apt_packages']
    if task == 'configure-git-webserver':
        packages = packages + ['expect']
    missing = []
    for package in packages:
        q = subprocess.run(['dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True)
        if q.returncode or q.stdout != 'install ok installed':
            missing.append(package)
    if missing:
        debs = sorted((ROOT / 'debs' / task).glob('*.deb'))
        if not debs:
            raise RuntimeError('uncached_system_dependencies:' + ','.join(missing))
        print('offline bootstrap: installing cached original system dependencies',flush=True)
        # Same single-UID workaround already used by the original harness's
        # apt/dpkg wrappers; do not preload it into the actual benchmark tests.
        env={**os.environ,'LD_PRELOAD':str(ROOT/'singleuid-compat.so'),'DEBIAN_FRONTEND':'noninteractive'}
        subprocess.run(['dpkg','-i',*map(str,debs)],env=env,check=True,timeout=180)
    if plan['runtime'] == 'standalone':
        venv = ROOT / 'venvs/official-script'
        run([ROOT / 'uv', 'venv', '--offline', '--python', ROOT / 'python/bin/python3', venv])
        python = str(venv / 'bin/python')
    else:
        python = shutil.which(plan['executable'])
        if not python:
            raise RuntimeError('native_test_python_missing')
    if plan['requirements']:
        run([ROOT / 'uv', 'pip', 'install', '--offline', '--no-index', '--break-system-packages',
             '--find-links', ROOT / 'wheels', '--python', python, *plan['requirements']])
    run([python, '-P', '-c', 'import pytest; import importlib.metadata as m; '
         'assert m.version("pytest-json-ctrf")=="0.3.5"'])
    if task == 'configure-git-webserver':
        helper = Path('/tests/verify.sh')
        original = helper.read_text()
        bootstrap = 'apt-get update\nDEBIAN_FRONTEND=noninteractive apt-get install -y expect\n'
        if original.count(bootstrap) != 1 or not shutil.which('expect'):
            raise RuntimeError('unrecognized_webserver_test_bootstrap')
        helper.write_text(original.replace(bootstrap, '# expect provided from the offline dependency cache\n'))
    os.environ.update(RSI_PYTEST_PYTHON=python, RSI_PYTEST_WRAPPER=str(ROOT / 'pytest_entry.py'),
                      RSI_GRADING_LOG=str(LOG), PYTHONSAFEPATH='1')
    if plan['runtime'] == 'standalone':
        os.environ['PATH'] = str(Path(python).parent) + os.pathsep + os.environ['PATH']
    if plan['ocaml_fixture']:
        repo = ROOT / 'resources/ocaml.git'
        commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse',
            translator.OCAML_TAG + '^{commit}'], text=True).strip()
        if commit != payload['extra_resources']['ocaml_commit']:
            raise RuntimeError('offline_ocaml_fixture_changed')
        os.environ.update(GIT_CONFIG_COUNT='2',
            GIT_CONFIG_KEY_0='url.' + repo.as_uri() + '.insteadOf',
            GIT_CONFIG_VALUE_0=translator.OCAML_URL,
            GIT_CONFIG_KEY_1='protocol.file.allow', GIT_CONFIG_VALUE_1='always')
    # Only the result destination changes. cp/cd/build/original-repo tests remain.
    body = plan['script'].replace('/logs/verifier', str(LOG))
    (LOG / 'offline-test.sh').write_text(body)
    completed = subprocess.run(['/bin/bash', str(LOG / 'offline-test.sh')], timeout=540)
    exits = [json.loads(line) for line in (LOG / 'pytest-exits.jsonl').read_text().splitlines()]
    if len(exits) != plan['pytest_calls'] or any(e['exit_code'] not in (0, 1) for e in exits):
        raise RuntimeError('pytest_infrastructure_exit:' + repr(exits))
    expected_reward = int(all(e['exit_code'] == 0 for e in exits))
    if (LOG / 'reward.txt').read_text().strip() != str(expected_reward):
        raise RuntimeError('official_shell_reward_disagreement')
    report = json.loads((LOG / 'ctrf.json').read_text())
    if plan['pytest_calls'] == 2:
        # Preserve both original reports; aggregate for the existing result reader.
        shutil.copy2(LOG / 'ctrf.json', LOG / 'additional-ctrf.json')
        original = json.loads((LOG / 'original-repo-ctrf.json').read_text())
        report['results']['tests'] += original['results']['tests']
        for key in ('tests', 'passed', 'failed', 'skipped', 'pending', 'other'):
            report['results']['summary'][key] = report['results']['summary'].get(key, 0) + original['results']['summary'].get(key, 0)
        (LOG / 'ctrf.json').write_text(json.dumps(report))
    summary = report['results']['summary']
    tests = report['results']['tests']
    if not tests or len(tests) != summary['tests'] or any(t['status'] not in ('passed', 'failed') for t in tests):
        raise RuntimeError('incomplete_official_test_execution:' + repr(summary))
    if expected_reward != int(summary['failed'] == 0):
        raise RuntimeError('pytest_exit_report_disagreement')
    (LOG / 'offline-result.json').write_text(json.dumps({'status': 'complete', 'task': task,
        'profile': cfg['profile'], 'reward': expected_reward, 'summary': summary,
        'official_test_hashes': cfg['tests'], 'python': python,
        'official_workflow_preserved': True, 'bootstrap_replacements': plan['bootstrap_replacements'],
        'shell_exit': completed.returncode, 'pytest_exits': exits}))
