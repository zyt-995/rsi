"""Verify a portable experiment against the frozen source-machine software/config contract.

No model calls or credentials are used. This checks reproducible inputs, not equality
of remote model outputs, hardware throughput, or historical unfinished checkpoints.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from .paths import REPO, docker_env


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def required_packages():
    return dict(line.split('==', 1) for line in (REPO / 'requirements.lock').read_text().splitlines()
                if line and not line.startswith('#'))


def session_errors(session):
    """Compare effective run settings, not just the templates that created them."""
    session = Path(session)
    manifest = read(session / 'manifest.json')
    roots = [Path(manifest['reuse_controls'][0]['root'])] + [Path(r['root']) for r in manifest['runs']]
    errors = []
    control = read(roots[0] / 'configuration.json')
    expected_models = {name: read(REPO / 'configs' / name / 'model.json') for name in ('deepseek', 'gpt')}
    model = next((name for name, cfg in expected_models.items() if cfg['model']['id'] == control['model']['id']), None)
    if model is None:
        return ['session model is not in the frozen experiment profiles']
    reference = expected_models[model]
    template = read(REPO / 'configs/templates/control.json')
    seen = []
    for root in roots:
        cfg = read(root / 'configuration.json')
        arms = list(cfg['arms'])
        seen += arms
        label = ','.join(arms)
        actual_model = {k: v for k, v in cfg['model'].items() if k != 'tokenizer_sha256'}
        if actual_model != reference['model']:
            errors.append(label + ': model/provider/sampling settings differ')
        if digest(root / 'tokenizer.json') != cfg['model'].get('tokenizer_sha256'):
            errors.append(label + ': tokenizer does not match run config')
        expected_tokenizer = ((REPO / 'configs/deepseek/tokenizer.sha256').read_text().strip()
                              if model == 'deepseek' else digest(REPO / 'configs/gpt/tokenizer.json'))
        if digest(root / 'tokenizer.json') != expected_tokenizer:
            errors.append(label + ': tokenizer differs from reference profile')
        for field in ('solve', 'accounting', 'chain_protocol'):
            if cfg[field] != reference[field]:
                errors.append(label + ': ' + field + ' differs')
        for arm, params in cfg['arms'].items():
            path = REPO / 'configs/methods' / (arm + '.json')
            if not path.is_file():
                errors.append(label + ': unsupported method'); continue
            expected = read(path)
            expected = expected.get('parameters', expected)
            actual = {k: v for k, v in params.items() if k != 'paired_with' or k in expected}
            if actual != expected:
                errors.append(arm + ': update/selection/memory parameters differ')
        # Paths, proxy routes, per-session ports and labels may vary by machine.
        operational = {'gateway_port', 'arm_concurrency', 'model_http_proxy', 'host_outbound_proxy',
                       'task_outbound_proxy', 'scheduling', 'validation_transport_epoch'}
        for key, value in template['runtime'].items():
            if key not in operational and cfg['runtime'].get(key) != value:
                errors.append(label + ': runtime.' + key + ' differs')
        extra = set(cfg['runtime']) - set(template['runtime'])
        if extra:
            errors.append(label + ': extra runtime overrides: ' + ','.join(sorted(extra)))
        if cfg['runtime'].get('task_outbound_proxy'):
            errors.append(label + ': solver network proxy must remain disabled')
    expected_arms = {'kira-static', 'meta-harness', 'experience-notes', 'failure-only-notes', 'evoskill'}
    if len(seen) != 5 or set(seen) != expected_arms:
        errors.append('session must contain each of the five reference methods once')
    return errors


def verify(session=None, software_only=False):
    reference = read(REPO / 'upstream/reproduction-reference.json')
    errors = []
    def check(ok, message):
        if not ok:
            errors.append(message)
    check(platform.python_version() == reference['python'], 'Python patch version differs: ' + platform.python_version())
    check(platform.system() == 'Linux' and platform.machine() == 'x86_64', 'Reference requires Linux x86_64')
    for name, version in required_packages().items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = 'missing'
        check(actual == version, f'Python package {name}: expected {version}, found {actual}')
    for name, expected in reference['files'].items():
        path = REPO / name
        check(path.is_file() and digest(path) == expected, 'Frozen source/config changed or missing: ' + name)
    upstream = read(REPO / 'upstream/manifest.json')
    for repo in upstream['repositories']:
        path = REPO / repo['path']
        def git(*args):
            return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL)
        try:
            check(git('rev-parse', 'HEAD').decode().strip() == repo['commit'], 'Upstream commit differs: ' + repo['name'])
            diff = git('diff', '--binary', 'HEAD')
            expected_diff = reference['upstream_diff_sha256'][repo['name']]
            check(hashlib.sha256(diff).hexdigest() == expected_diff, 'Upstream working tree/patch differs: ' + repo['name'])
            check(not git('ls-files', '--others', '--exclude-standard').strip(), 'Untracked upstream files: ' + repo['name'])
        except subprocess.CalledProcessError:
            errors.append('Upstream checkout unavailable: ' + repo['name'])
    if not software_only:
        try:
            env = docker_env()
            commands = {'docker_client': ['docker', '--version'],
                        'docker_server': ['docker', 'version', '--format', '{{.Server.Version}}'],
                        'compose': ['docker', 'compose', 'version', '--short'],
                        'bwrap': ['bwrap', '--version'], 'uv': ['uv', '--version'],
                        'go': [os.environ.get('RSI_GO_BINARY') or 'go', 'version'],
                        'claude': ['claude', '--version']}
            for name, command in commands.items():
                try:
                    p = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
                    check(p.returncode == 0 and p.stdout.strip() == reference['tools'][name],
                          'System tool version differs or unavailable: ' + name)
                except (OSError, subprocess.TimeoutExpired):
                    errors.append('System tool unavailable: ' + name)
            binary = shutil.which('claude')
            check(bool(binary) and digest(Path(binary).resolve()) == reference['claude_binary_sha256'],
                  'Native Claude executable differs from reference binary')
            p = subprocess.run(['bwrap', '--ro-bind', '/', '/', '--unshare-user', '/bin/true'],
                               env=env, capture_output=True, timeout=10)
            check(p.returncode == 0, 'Required sandbox user namespace unavailable')
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            errors.append('System prerequisite check failed: ' + type(exc).__name__)
    if session is not None:
        try:
            errors += session_errors(session)
        except (OSError, KeyError, ValueError) as exc:
            errors.append('Session contract unavailable or malformed: ' + type(exc).__name__)
    return {'passed': not errors, 'reference': reference['id'], 'errors': errors,
            'scope': 'software_only' if software_only else 'software_and_system_tools',
            'session_checked': session is not None, 'paid_model_calls': 0,
            'limits': 'Does not assert identical remote model outputs, physical hardware, timing, cache or historical learning state.'}


def require_match(session):
    result = verify(session)
    if not result['passed']:
        raise RuntimeError('Reference reproduction check failed before paid launch: ' + '; '.join(result['errors']))
    return result
