#!/usr/bin/env python3
"""Complete the retained engineering control with its missing planarity dependency."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_resilient import grading
from offline_verifier.runner import docker


def main(output, planarity_version):
    source = Path('/data2/ytzhang/rsi-harness-runs/cython-positive-control-20261001-v2')
    out = output.resolve()
    if out.exists():
        raise RuntimeError('control_directory_already_exists')
    out.mkdir()
    os.environ.update(runner_environment(8040))
    target = read_json(source / 'container.json')
    cid = target['id']
    info = grading.inspect_container(cid, target['image'])
    if info['HostConfig']['NetworkMode'] != 'none':
        raise RuntimeError('control_requires_network_none')
    metadata = json.load(urllib.request.urlopen(
        'https://pypi.org/pypi/planarity/' + planarity_version + '/json', timeout=20))
    artifact = next(x for x in metadata['urls'] if x['packagetype'] == 'sdist')
    path = out / artifact['filename']
    urllib.request.urlretrieve(artifact['url'], path)
    if file_hash(path) != artifact['digests']['sha256']:
        raise RuntimeError('dependency_archive_hash_mismatch')
    atomic_json(out / 'started.json', {'time': time.time(), 'source_control': str(source),
        'source_failure_sha256': file_hash(source / 'positive/ctrf.json'), 'container': cid,
        'dependency': artifact, 'formal_scores': 0, 'model_calls': 0,
        'script_sha256': file_hash(Path(__file__)), 'tests_modified': False})
    if not info['State']['Running']:
        docker(['start', cid])
    # Tar normalizes ownership for this rootless, single-UID Docker daemon.
    with subprocess.Popen(['tar', '-cf', '-', '-C', str(out), path.name], stdout=subprocess.PIPE) as archive:
        subprocess.run(['docker', 'exec', '-i', cid, 'tar', '--no-same-owner', '-xf', '-', '-C', '/control'],
                       stdin=archive.stdout, check=True, timeout=60)
        archive.stdout.close()
        if archive.wait():
            raise RuntimeError('dependency_upload_failed')
    try:
        with (out / 'dependency-build.log').open('xb') as log:
            subprocess.run(['docker', 'exec', cid, 'python', '-m', 'pip', 'install', '--no-index',
                '--no-deps', '--no-build-isolation', '/control/' + path.name],
                stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
        grader = Path('/data2/ytzhang/rsi-harness-runs/resilience-acceptance-20261001/grader-manifest.json')
        m = grading.load_manifest(grader, file_hash(grader))
        positive = grading.grade_container(cid, 'build-cython-ext', m, out / 'positive')
        if positive['reward'] != 1:
            raise RuntimeError('positive_control_not_passing')
        docker(['exec', cid, 'python', '-m', 'pip', 'uninstall', '-y', 'pyknotid'])
        negative = grading.grade_container(cid, 'build-cython-ext', m, out / 'negative')
        if negative['reward'] != 0:
            raise RuntimeError('negative_control_not_rejected')
        atomic_json(out / 'summary.json', {'passed': True, 'time': time.time(),
            'positive': 1, 'negative': 0, 'model_calls': 0, 'formal_scores': 0,
            'grader_sha256': file_hash(grader), 'container_network': 'none',
            'assertions_changed': False, 'source_control': str(source)})
        docker(['rm', '-f', cid])
        atomic_json(out / 'cleanup.json', {'removed_container': cid, 'time': time.time()})
    except Exception as exc:
        atomic_json(out / 'failure.json', {'time': time.time(), 'reason': str(exc), 'container_retained': cid})
        docker(['stop', '--time', '5', cid])
        raise


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--planarity-version', required=True)
    main(**vars(p.parse_args()))
