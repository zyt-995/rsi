"""Prepare one complete chain at a time using cached images and offline graders."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time

from harbor.models.task.task import Task
from offline_verifier import runner, profiles
from rsi_harness.clean_campaign import DATASET, OLD_ROOT, missing_assets
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import PROJECT, atomic_json, file_hash, fingerprint
from self_skill_rsi_accel.environment import runner_environment
from . import grading
from self_skill_rsi_storage import storage
from .bootstrap import translate

BASE = Path('/data2/ytzhang/rsi-harness-runs/offline-control-full-20261002-v3')


from .infrastructure import resolve_entry, runtime_preflight


def cache_packages(cid, packages, dest):
    """Reuse the signed-index/hash-verified downloader with the independent proxy."""
    from self_skill_rsi_resume import offline
    original = offline.subprocess.run
    def independent(args, *a, **kw):
        args = [str(v).replace('http://127.0.0.1:17897', 'http://127.0.0.1:17898') for v in args]
        return original(args, *a, **kw)
    offline.subprocess.run = independent
    try: offline.cache_apt_packages(cid, packages, dest)
    finally: offline.subprocess.run = original


def dependencies(prefix, entries, plans, output):
    env = os.environ.copy()
    env.update(HTTP_PROXY='http://127.0.0.1:17898', HTTPS_PROXY='http://127.0.0.1:17898',
               http_proxy='http://127.0.0.1:17898', https_proxy='http://127.0.0.1:17898')
    for e in entries:
        task, plan = e['task'], plans[e['task']]
        if plan['requirements']:
            minor = plan['python_minor'] or '3.13'
            if minor == '3.12' and not (prefix / 'python312').exists():
                shutil.copytree(PROJECT / '.deps/python/cpython-3.12.14-linux-x86_64-gnu', prefix / 'python312', symlinks=True)
            receipt = output / 'dependencies' / f'{task}.json'
            if not receipt.exists():
                args = [str(prefix / 'python/bin/python3'), '-m', 'pip', 'download', '--disable-pip-version-check',
                    '--only-binary=:all:', '--python-version', minor, '--dest', str(prefix / 'wheels'),
                    '--find-links', str(prefix / 'wheels'), '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple', *plan['requirements']]
                log = output / f'download-{task}.log'
                with log.open('ab') as stream:
                    subprocess.run(args, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=900)
                atomic_json(receipt, {'requirements': plan['requirements'], 'python_minor': minor})
        if not plan['apt_packages']: continue
        receipt = output / 'system-dependencies' / f'{task}.json'
        if receipt.exists(): continue
        cid = runner.docker(['run', '-d', '--network', 'host', '-e', 'http_proxy=http://10.0.2.2:17898',
            '-e', 'https_proxy=http://10.0.2.2:17898', '--entrypoint', '/bin/sh', e['image_id'], '-c', 'sleep infinity']).stdout.strip()
        try:
            missing = []
            for package in plan['apt_packages']:
                result = subprocess.run(['docker', 'exec', cid, 'dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True, timeout=20)
                if result.returncode or result.stdout != 'install ok installed': missing.append(package)
            if missing:
                from self_skill_rsi_resume.offline import apt_prepare
                apt_prepare(cid, ['update'], output / 'preparation-logs' / f'{task}-apt.log')
                cache_packages(cid, missing, prefix / 'debs' / task)
            atomic_json(receipt, {'image': e['image_id'], 'missing': missing, 'packages': plan['apt_packages']})
        finally:
            runner.docker(['stop', '--time', '1', cid]); runner.docker(['rm', cid])


def prepare(root, chain):
    output = root / 'environments-recovered' / chain
    if (root / 'environment-ready' / (chain + '.json')).exists():
        return read_json(Path(read_json(root / 'environment-ready' / (chain + '.json'))['acceptance']))
    accepted = output / 'acceptance.json'
    if accepted.exists() and not read_json(accepted)['passed']:
        raise RuntimeError('failed_acceptance_requires_versioned_repair')
    output.mkdir(parents=True, exist_ok=True)
    queue = read_json(root / 'queue.json')
    requested = [e for e in queue['remaining'] if e['chain'] == chain]
    entries = [resolve_entry(e, root / 'environment-readiness') for e in requested]
    plans = {e['task']: translate((Path(e['path']) / 'tests/test.sh').read_text()) for e in entries}
    isolation_source = PROJECT / 'terminal_offline_expand' / ('isolated_v2.py' if chain == 'configure-git-webserver' else 'isolated.py')
    atomic_json(output / 'registration.json', {'entries': entries, 'plans': plans, 'formal_scores': False, 'model_calls': 0})
    stage = output / 'bundle/staging'
    prefix = stage / 'opt/rsi-offline-v1'
    if not stage.exists(): shutil.copytree(BASE / 'bundle/staging', stage, symlinks=True)
    if not (output / 'grader-manifest.json').exists():
        for original, name in [('grade.py', 'original_grade.py'), ('isolated.py', 'original_bridge.py')]:
            if not (prefix / name).exists(): shutil.copy2(prefix / original, prefix / name)
        for src, dst in [('grade.py', 'grade.py'), (isolation_source, 'isolated.py'), ('bootstrap.py', 'extension_bootstrap.py')]:
            shutil.copy2(Path(__file__).parent / src, prefix / dst)
        dependencies(prefix, entries, plans, output)
        payload = read_json(prefix / 'payload.json')
        for e in entries:
            payload['tasks'][e['task']] = {'profile': 'extension-script', 'path': e['path'], 'image_id': e['image_id'],
                'working_directory': e['working_directory'], 'task_checksum': e['task_checksum'], 'tests': profiles.test_hashes(Path(e['path'])), 'bootstrap': plans[e['task']]}
        payload['files'] = {str(p.relative_to(prefix)): file_hash(p) for p in sorted(prefix.rglob('*'))
                            if p.is_file() and not p.is_symlink() and p.name != 'payload.json'}
        atomic_json(prefix / 'payload.json', payload)
        bundle = output / 'bundle/runtime.tar.gz'
        with tarfile.open(bundle, 'w:gz', compresslevel=1) as archive:
            def owner(info):
                info.uid = info.gid = 0; info.uname = info.gname = ''; return info
            archive.add(stage / 'opt', arcname='opt', filter=owner)
        manifest = copy.deepcopy(read_json(BASE / 'bundle/manifest.json'))
        manifest.update(bundle=str(bundle), bundle_sha256=file_hash(bundle), payload_sha256=file_hash(prefix / 'payload.json'),
            tasks=payload['tasks'], source_manifest=str(output / 'registration.json'), source_manifest_sha256=file_hash(output / 'registration.json'))
        manifest['code'].update({str(p.resolve()): file_hash(p) for p in (Path(__file__).parent / name for name in ('grade.py', str(isolation_source), 'bootstrap.py', 'safe_entry.py'))})
        atomic_json(output / 'bundle/manifest.json', manifest)
        grading.prepare(output / 'bundle/manifest.json', output / 'grader-manifest.json')
    safe = grading.load_manifest(output / 'grader-manifest.json', file_hash(output / 'grader-manifest.json'))
    def check(e):
        folder = output / 'acceptance' / e['task']
        if (folder / 'summary.json').exists(): return read_json(folder / 'summary.json')
        if folder.exists(): raise RuntimeError('unfinished_smoke_requires_review:' + e['task'])
        cid = runner.docker(['run', '-d', '--network', 'none', '--entrypoint', '/bin/sh', e['image_id'], '-c', 'sleep infinity']).stdout.strip()
        atomic_json(folder / 'container.json', {'container': cid, 'image': e['image_id'], 'model_calls': 0})
        try:
            result = grading.grade_container(cid, e['task'], safe, folder / 'grade')
            value = {'task': e['task'], 'passed': True, 'unmodified_reward': result['reward'], 'tests': result['summary']['tests']}
        except Exception as exc:
            value = {'task': e['task'], 'passed': False, 'error': type(exc).__name__ + ':' + str(exc)[-2200:]}
        finally:
            runner.docker(['stop', '--time', '1', cid]); runner.docker(['rm', cid])
            atomic_json(folder / 'cleanup.json', {'removed': True, 'container': cid, 'time': time.time()})
        atomic_json(folder / 'summary.json', value)
        print(json.dumps(value), flush=True)
        return value
    with ThreadPoolExecutor(max_workers=2) as pool: rows = list(pool.map(check, entries))
    value = {'passed': all(x['passed'] for x in rows), 'time': time.time(), 'chain': chain,
        'entries': entries, 'grader': str(output / 'grader-manifest.json'),
        'grader_sha256': file_hash(output / 'grader-manifest.json'), 'tasks': rows,
        'model_calls': 0, 'official_assertions_changed': False}
    atomic_json(accepted, value)
    if value['passed']:
        for e in entries:
            # Reuse the same content-addressed image, but record fresh offline grading acceptance.
            runtime = runtime_preflight(root, e, output / 'grader-manifest.json', value['grader_sha256'], output)
            ticket = {'status': 'ready', 'task': e['task'], 'runtime': runtime, 'model_calls': 0}
            atomic_json(root / 'environment-readiness/_infra/readiness' / f"{e['task']}.json",
                {**ticket, 'image': e['image'], 'image_id': e['image_id'], 'fingerprint': e['environment_fingerprint'],
                 'offline_acceptance': str(accepted), 'offline_acceptance_sha256': file_hash(accepted)})
        atomic_json(root / 'environment-ready' / f'{chain}.json', {'acceptance': str(accepted), 'sha256': file_hash(accepted)})
    return value


def run(root, limit=None, chain=None):
    os.environ.update(runner_environment(8041)); storage.activate()
    queue = read_json(root / 'queue.json')
    chains = [chain] if chain else (queue['chain_order'][:limit] if limit else queue['chain_order'])
    for chain in chains:
        atomic_json(root / 'environment-progress.json', {'state': 'preparing', 'chain': chain, 'time': time.time()})
        try:
            value = prepare(root, chain)
            if not value['passed']: raise RuntimeError('offline_grader_smoke_failed')
        except Exception as exc:
            atomic_json(root / 'environment-errors' / f'{chain}.json', {'chain': chain, 'time': time.time(),
                'reason': type(exc).__name__ + ':' + str(exc)[-2400:], 'model_calls': 0, 'is_algorithmic_zero': False})
            print('ENVIRONMENT_NEEDS_REVIEW ' + chain + ': ' + str(exc)[-500:], flush=True)
    atomic_json(root / 'environment-progress.json', {'state': 'scan_complete', 'time': time.time(),
        'ready_chains': len(list((root / 'environment-ready').glob('*.json'))),
        'errors': len(list((root / 'environment-errors').glob('*.json')))})


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', type=Path, required=True); p.add_argument('--limit', type=int); p.add_argument('--chain')
    a = p.parse_args(); run(a.root.resolve(), a.limit, a.chain)

