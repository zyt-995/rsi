"""Runs in the isolated namespace. No network-capable bootstrap or retry."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path('/opt/rsi-offline-v1')
LOG = Path('/logs/verifier')


def run(args):
    print('offline dependency setup:', ' '.join(args), flush=True)
    subprocess.run(args, check=True, timeout=180)


def browser_infrastructure_failure(tests):
    # Pytest includes unexecuted source lines in tracebacks. Only inspect the
    # actual exception lines, never strings appearing in the test's source.
    markers = ('SessionNotCreatedException', 'NoSuchDriverException',
               'WebDriverException', 'AssertionError: Filter failed with error:')
    return any(any(marker in line for marker in markers)
               for test in tests for line in test.get('trace', '').splitlines()
               if line.startswith('E '))


def grade(task):
    payload = json.loads((ROOT / 'payload.json').read_text())
    cfg = payload['tasks'][task]
    for rel, expected in cfg['tests'].items():
        if hashlib.sha256((Path('/tests') / rel).read_bytes()).hexdigest() != expected:
            raise RuntimeError('official_test_changed:' + rel)
    profile = cfg['profile']
    image_python = shutil.which('python3')
    if not image_python:
        raise RuntimeError('task_python_not_available')
    # Stats/HTML/base-chess originally use an isolated uvx CPython 3.13 runtime.
    # Cython must use the task interpreter so agent-built extensions remain visible.
    if profile in ('html', 'stats', 'plain'):
        venv = ROOT / 'venvs' / profile
        run([str(ROOT / 'uv'), 'venv', '--offline', '--python', str(ROOT / 'python/bin/python3'), str(venv)])
        python = str(venv / 'bin/python')
    else:
        python = image_python
    if profile != 'native':
        run([str(ROOT / 'uv'), 'pip', 'install', '--offline', '--no-index', '--break-system-packages',
             '--find-links', str(ROOT / 'wheels'), '--constraint', str(ROOT / 'constraints.txt'),
             '--python', python, *payload['profiles'][profile]])
    os.environ['PATH'] = str(Path(python).parent) + os.pathsep + os.environ['PATH']
    run([python, '-c', 'import importlib.metadata as m; '
         'assert m.version("pytest")=="8.4.1"; assert m.version("pytest-json-ctrf")=="0.3.5"'])
    replacements = []
    if profile == 'cython':
        # Same git tag, resolved to a hash-checked local bare repository.
        os.environ.update(GIT_CONFIG_COUNT='2',
            GIT_CONFIG_KEY_0='url.file:///opt/rsi-offline-v1/resources/pyknotid.git.insteadOf',
            GIT_CONFIG_VALUE_0='https://github.com/SPOCKnots/pyknotid.git',
            GIT_CONFIG_KEY_1='protocol.file.allow', GIT_CONFIG_VALUE_1='always')
        # A missing grader fixture must never become a task failure. Exercise the
        # same git transport / zip extraction BEFORE letting pytest judge output.
        with tempfile.TemporaryDirectory(prefix='offline-fixture-check-') as temp:
            if task.endswith('-EVO-4'):
                archive = ROOT / 'resources/pyknotid-0.5.3.zip'
                if hashlib.sha256(archive.read_bytes()).hexdigest() != payload['fixture']['zip_sha256']:
                    raise RuntimeError('offline_zip_fixture_changed')
                with zipfile.ZipFile(archive) as z:
                    if z.testzip() is not None:
                        raise RuntimeError('offline_zip_fixture_corrupt')
                    z.extractall(temp)
                if not (Path(temp) / 'pyknotid-0.5.3/tests').is_dir():
                    raise RuntimeError('offline_zip_tests_missing')
            else:
                run(['git', 'clone', '--depth', '1', '--branch', '0.5.3', payload['fixture']['git_url'], temp])
                actual = subprocess.check_output(['git', '-C', temp, 'rev-parse', 'HEAD'], text=True).strip()
                if actual != payload['fixture']['commit'] or not (Path(temp) / 'tests').is_dir():
                    raise RuntimeError('offline_git_fixture_changed')
        if task.endswith('-EVO-4'):
            test = Path('/tests/test_outputs.py')
            original = test.read_text()
            url = payload['fixture']['zip_url']
            if original.count(url) != 1:
                raise RuntimeError('zip_fixture_reference_changed')
            # Only the resource locator is changed; no assertions or tests removed.
            test.write_text(original.replace(url, (ROOT / 'resources/pyknotid-0.5.3.zip').as_uri()))
            replacements.append({'kind': 'fixture_url_only', 'from': url,
                                 'to': (ROOT / 'resources/pyknotid-0.5.3.zip').as_uri()})
    command = [python, '-m', 'pytest', '--ctrf', str(LOG / 'ctrf.json'), '/tests/test_outputs.py', '-rA']
    print('offline official tests:', ' '.join(command), flush=True)
    completed = subprocess.run(command, timeout=540)
    if completed.returncode not in (0, 1):
        raise RuntimeError('pytest_infrastructure_exit:' + str(completed.returncode))
    report = json.loads((LOG / 'ctrf.json').read_text())
    summary = report['results']['summary']
    if type(summary.get('tests')) is not int or summary['tests'] <= 0 or any(summary.get(k, 0) for k in ('other', 'pending', 'skipped')):
        raise RuntimeError('invalid_or_incomplete_test_report:' + str(summary))
    expected = len(report['results']['tests'])
    if expected != summary['tests'] or summary.get('passed', 0) + summary.get('failed', 0) != expected:
        raise RuntimeError('inconsistent_test_report')
    if (completed.returncode == 0) != (summary.get('failed', 0) == 0):
        raise RuntimeError('pytest_exit_report_disagreement')
    if profile == 'html' and browser_infrastructure_failure(report['results']['tests']):
        raise RuntimeError('browser_or_filter_infrastructure_failed')
    reward = int(completed.returncode == 0)
    (LOG / 'reward.txt').write_text(str(reward))
    (LOG / 'offline-result.json').write_text(json.dumps({'status': 'complete', 'task': task,
        'profile': profile, 'reward': reward, 'summary': summary, 'resource_replacements': replacements,
        'fixture_commit': payload['fixture']['commit'], 'python': python, 'official_test_hashes': cfg['tests']}))


if __name__ == '__main__':
    try:
        grade(sys.argv[1])
    except Exception as exc:
        (LOG / 'offline-error.json').write_text(json.dumps({'error': type(exc).__name__ + ':' + str(exc),
                                                          'is_algorithmic_zero': False}))
        raise
