#!/usr/bin/env python3
"""Disposable Cython positive/negative controls. NEVER consumed by learners.

Only this engineering-control container gets cached sources and dependencies.
No formal task image, solver environment, test assertion or skill is modified.
"""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resilient import grading
from self_skill_rsi_accel.environment import runner_environment
from offline_verifier.runner import docker

GRADER = Path('/data2/ytzhang/rsi-harness-runs/resilience-acceptance-20261001/grader-manifest.json')
RESOURCES = Path('/data2/ytzhang/rsi-harness-static-data/offline-verifier-v4/staging/opt/rsi-offline-v1/resources')
PACKAGES = ['setuptools==80.9.0', 'cython==3.1.3', 'wheel', 'numpy==2.3.0',
            'sympy==1.14.0', 'vispy', 'networkx', 'requests', 'tqdm', 'appdirs', 'scipy==1.16.1']


def upload(cid, files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for path, name in files:
            info = tar.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            with path.open('rb') as f:
                tar.addfile(info, f)
    subprocess.run(['docker', 'exec', '-i', cid, 'tar', '--no-same-owner', '-xf', '-', '-C', '/'],
                   input=data.getvalue(), check=True, capture_output=True, timeout=120)


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'started.json').exists():
        raise RuntimeError('control_attempt_already_exists; use_a_new_directory')
    os.environ.update(runner_environment(8040))
    manifest = grading.load_manifest(GRADER, file_hash(GRADER))
    atomic_json(output / 'started.json', {'time': time.time(), 'formal_scores': 0,
                'model_calls': 0, 'script_sha256': file_hash(Path(__file__))})
    cache = output / 'wheels'
    cache.mkdir()
    cmd = [str(PROJECT / '.venv-terminal/bin/pip'), 'download', '--only-binary=:all:',
           '--python-version', '3.13', '--implementation', 'cp', '--abi', 'cp313',
           '--platform', 'manylinux2014_x86_64', '--platform', 'manylinux_2_28_x86_64',
           '--dest', str(cache), '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple',
           '--find-links', str(RESOURCES.parent / 'wheels'), *PACKAGES]
    with (output / 'download.log').open('xb') as log:
        subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
    wheels = list(cache.glob('*.whl'))
    atomic_json(output / 'cache.json', {'files': {str(p): file_hash(p) for p in wheels}, 'packages': PACKAGES})
    cfg = manifest['tasks']['build-cython-ext']
    cid = docker(['create', '--network', 'none', '--name', 'rsi-cython-control-' + uuid.uuid4().hex[:10],
                  '--entrypoint', 'sleep', cfg['image_id'], 'infinity']).stdout.strip()
    atomic_json(output / 'container.json', {'id': cid, 'image': cfg['image_id']})
    try:
        docker(['start', cid])
        upload(cid, [(p, 'control/wheels/' + p.name) for p in wheels])
        # A bare local git fixture is streamed rather than fetched from the network.
        with subprocess.Popen(['tar', '-cf', '-', '-C', str(RESOURCES), 'pyknotid.git'], stdout=subprocess.PIPE) as src:
            docker(['exec', cid, 'mkdir', '-p', '/control'])
            subprocess.run(['docker', 'exec', '-i', cid, 'tar', '--no-same-owner', '-xf', '-', '-C', '/control'],
                           stdin=src.stdout, check=True, timeout=120)
            src.stdout.close()
            if src.wait() != 0:
                raise RuntimeError('fixture_copy_failed')
        solution = Path(cfg['path']) / 'solution/solve.sh'
        upload(cid, [(solution, 'control/original-solve.sh')])
        # Use the original solution's edits/build, but install only runtime
        # dependencies needed by real tests, without online dependency resolution.
        prepare = '''set -eu
python -m pip install --no-index --find-links=/control/wheels numpy==2.3.0 setuptools==80.9.0 cython==3.1.3 wheel sympy==1.14.0 vispy networkx requests tqdm appdirs scipy==1.16.1
git config --global url.file:///control/pyknotid.git.insteadOf https://github.com/SPOCKnots/pyknotid.git
git config --global protocol.file.allow always
sed 's/pip install -e ./pip install --no-deps --no-build-isolation -e ./' /control/original-solve.sh | bash -e
cd /tmp
python -P -c 'import pyknotid; from pyknotid.spacecurves import chelpers,ccomplexity; from pyknotid import cinvariants; print(pyknotid.__file__)'
python -m pip freeze
'''
        with (output / 'build.log').open('xb') as log:
            subprocess.run(['docker', 'exec', '-e', 'PIP_NO_INDEX=1', '-e', 'PIP_FIND_LINKS=/control/wheels',
                            cid, 'bash', '-c', prepare], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
        positive = grading.grade_container(cid, 'build-cython-ext', manifest, output / 'positive')
        if positive['reward'] != 1:
            raise RuntimeError('known_solution_failed_positive_control')
        # Deliberately uninstall the deliverable, then verify that it is rejected.
        docker(['exec', cid, 'python', '-m', 'pip', 'uninstall', '-y', 'pyknotid'])
        negative = grading.grade_container(cid, 'build-cython-ext', manifest, output / 'negative')
        if negative['reward'] != 0:
            raise RuntimeError('missing_deliverable_not_rejected')
        atomic_json(output / 'summary.json', {'passed': True, 'positive': positive['reward'],
            'negative': negative['reward'], 'model_calls': 0, 'formal_scores': 0,
            'network': 'container_network_none_for_build_and_grading',
            'grader_sha256': file_hash(GRADER), 'time': time.time()})
    except Exception as exc:
        atomic_json(output / 'failure.json', {'time': time.time(), 'reason': str(exc),
            'formal_scores': 0, 'retained_container': cid})
        docker(['stop', '--time', '5', cid])
        raise
    else:
        docker(['rm', '-f', cid])
        atomic_json(output / 'cleanup.json', {'container': cid, 'removed': True, 'time': time.time()})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    main(p.parse_args().output.resolve())
