"""Materialize only registered LFS blobs, with a crash-safe provenance journal."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from harbor.models.task.task import Task
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash, fingerprint

REPO = Path('/home/ytzhang/rsi-harness/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo')
COMMIT = '18ecf86f67ad9208eccaf195c63ceb42f4095dc0'


def files(path):
    result = {}
    for p in sorted(path.rglob('*')):
        if not p.is_file() or '__pycache__' in p.parts or p.name == '.harbor-docker-build.lock': continue
        # Materialization changes files in place. Do not trust a stat-key cache:
        # some filesystems coalesce timestamps on two same-size rapid writes.
        with p.open('rb') as stream:
            result[str(p.relative_to(path))] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def validate(entry, journal):
    if journal['entry'] != entry: raise RuntimeError('materialization_entry_changed')
    expected = dict(journal['before_files'])
    expected.update({name: value['oid'] for name, value in journal['blobs'].items()})
    if files(Path(entry['path'])) != expected:
        raise RuntimeError('materialization_changed_nonasset_input:' + entry['task'])
    return {**entry, 'task_checksum': Task(Path(entry['path'])).checksum,
            'environment_fingerprint': fingerprint(Path(entry['path']))}


def materialize(root, entry):
    path = Path(entry['path']); journal_path = root / 'asset-materialization' / (entry['task'] + '.json')
    journal = read_json(journal_path)
    if journal is None:
        if Task(path).checksum != entry['task_checksum'] or fingerprint(path) != entry['environment_fingerprint']:
            raise RuntimeError('unregistered_input_change_before_materialization:' + entry['task'])
        blobs = {}
        for name in files(path):
            p = path / name
            if not name.startswith(('environment/', 'tests/')) or p.stat().st_size >= 1024: continue
            data = p.read_bytes()
            match = re.fullmatch(rb'version https://git-lfs.github.com/spec/v1\noid sha256:([a-f0-9]{64})\nsize ([0-9]+)\n?', data)
            if match:
                # The fetched object must be exactly the one pinned in Git, not an arbitrary URL.
                rel = p.relative_to(REPO).as_posix()
                original = subprocess.check_output(['git', '-C', str(REPO), 'show', COMMIT + ':' + rel])
                if original != data: raise RuntimeError('pointer_does_not_match_pinned_upstream_commit')
                blobs[name] = {'oid': match[1].decode(), 'size': int(match[2]), 'pointer_sha256': file_hash(p), 'repo_path': rel}
        journal = {'entry': entry, 'before_files': files(path), 'blobs': blobs, 'commit': COMMIT,
                   'started': time.time(), 'code_sha256': file_hash(Path(__file__)), 'model_calls': 0}
        atomic_json(journal_path, journal)
    cache = root / 'asset-cache'; cache.mkdir(parents=True, exist_ok=True)
    for name, blob in journal['blobs'].items():
        p = path / name
        if file_hash(p) not in (blob['pointer_sha256'], blob['oid']): raise RuntimeError('unexpected_asset_contents')
        target = cache / blob['oid']
        if not target.exists():
            part = cache / (blob['oid'] + '.part')
            url = 'https://huggingface.co/datasets/wufeiwu/Terminal-Bench-Evo/resolve/' + COMMIT + '/' + blob['repo_path']
            subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error', '--retry', '2',
                '--connect-timeout', '15', '--max-time', '180', '--proxy', 'http://127.0.0.1:17898',
                '--output', str(part), url], check=True, timeout=570)
            if part.stat().st_size != blob['size'] or file_hash(part) != blob['oid']:
                raise RuntimeError('lfs_download_size_or_sha256_mismatch')
            part.replace(target)
        if target.stat().st_size != blob['size'] or file_hash(target) != blob['oid']:
            raise RuntimeError('lfs_cache_corrupt')
        if file_hash(p) != blob['oid']:
            temporary = p.with_name(p.name + '.rsi-materializing')
            shutil.copy2(target, temporary); temporary.replace(p)
    resolved = validate(entry, journal)
    receipt = {'resolved': resolved, 'journal': str(journal_path), 'journal_sha256': file_hash(journal_path),
               'finished': time.time(), 'official_assertions_changed': False, 'model_calls': 0}
    receipt_path = root / 'asset-receipts' / (entry['task'] + '.json')
    if not receipt_path.exists(): atomic_json(receipt_path, receipt)
    return resolved


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path); p.add_argument('--chain')
    a = p.parse_args(); q = read_json(a.root / 'queue.json')
    for entry in q['remaining'][11:]:
        if a.chain and entry['chain'] != a.chain: continue
        try:
            value = materialize(a.root, entry)
            print(json.dumps({'task': entry['task'], 'status': 'materialized', 'checksum': value['task_checksum']}), flush=True)
        except Exception as exc:
            atomic_json(a.root / 'asset-errors' / (entry['task'] + '.json'), {'task': entry['task'], 'error': str(exc), 'time': time.time()})
            print(json.dumps({'task': entry['task'], 'status': 'error', 'error': str(exc)}), flush=True)
