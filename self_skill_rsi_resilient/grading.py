"""Grade the original filesystem with safe imports and attempt-specific logs."""
import io
import json
from pathlib import Path
import signal
import subprocess
import tarfile
import time
import uuid

from offline_verifier import runner as old
from offline_verifier.profiles import sha, test_hashes
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json

ENTRY = Path(__file__).with_name('safe_entry.py')
REMOTE = '/opt/rsi-resilient-v1/safe_entry.py'


def load_manifest(path, expected):
    path = Path(path)
    if sha(path) != expected:
        raise RuntimeError('resilient_grader_manifest_changed')
    m = read_json(path)
    base = old.load_manifest(m['base_manifest'], m['base_sha256'])
    if sha(ENTRY) != m['safe_entry_sha256']:
        raise RuntimeError('resilient_grader_entry_changed')
    return {**base, 'resilient': m}


def prepare(base_manifest, output):
    old.load_manifest(base_manifest, sha(base_manifest))
    if output.exists():
        raise RuntimeError('grading_manifest_already_exists')
    atomic_json(output, {'schema': 'offline-safe-imports-v1',
        'base_manifest': str(base_manifest.resolve()), 'base_sha256': sha(base_manifest),
        'safe_entry_sha256': sha(ENTRY), 'created': time.time(),
        'network': 'isolated_loopback_only', 'assertions_changed': False})


def inspect_container(container, image=None):
    info = json.loads(old.docker(['inspect', container]).stdout)[0]
    if info['Id'] != container or (image and info['Image'] != image):
        raise RuntimeError('retained_container_identity_mismatch')
    return info


def upload_entry(container):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        info = tar.gettarinfo(str(ENTRY), arcname=REMOTE.lstrip('/'))
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        with ENTRY.open('rb') as stream:
            tar.addfile(info, stream)
    subprocess.run(['docker', 'exec', '-i', container, 'tar', '--no-same-owner', '-xf', '-', '-C', '/'],
                   input=data.getvalue(), capture_output=True, check=True, timeout=120)


def grade_container(container, task, manifest, output, timeout=570):
    output = Path(output)
    if (output / 'attempt.json').exists():
        raise RuntimeError('grading_attempt_exists; never_overwrite')
    output.mkdir(parents=True, exist_ok=True)
    cfg = manifest['tasks'][task]
    if test_hashes(Path(cfg['path'])) != cfg['tests']:
        raise RuntimeError('official_test_hash_changed')
    info = inspect_container(container, cfg['image_id'])
    if not info['State']['Running']:
        raise RuntimeError('retained_container_not_running')
    token = uuid.uuid4().hex
    remote_log = '/logs/rsi-grading-attempts/' + token
    atomic_json(output / 'attempt.json', {'container': container, 'task': task, 'started': time.time(),
        'remote_log': remote_log, 'image': info['Image'], 'labels': info['Config'].get('Labels', {})})
    deadline = time.monotonic() + timeout
    with Path(manifest['bundle']).open('rb') as archive:
        subprocess.run(['docker', 'exec', '-i', container, 'tar', '--no-same-owner', '-xzf', '-', '-C', '/'],
                       stdin=archive, capture_output=True, check=True, timeout=min(timeout, 120))
    upload_entry(container)
    old.docker(['exec', container, 'mkdir', '-p', '/tests', '/logs/rsi-grading-attempts'])
    old.upload_tests(container, Path(cfg['path']) / 'tests')
    pid = old.host_pid(info['State']['Pid'])
    if Path(f'/proc/{pid}/root/etc/hostname').read_text().strip() != info['Config']['Hostname']:
        raise RuntimeError('container_pid_identity_mismatch')
    command = ['nsenter', '--preserve-credentials', '--target', str(pid), '--user', '--mount',
               '--pid', '--root', '--wd', '--', '/opt/rsi-offline-v1/python/bin/python3',
               '-I', REMOTE, task, remote_log]
    with (output / 'test-stdout.txt').open('xb') as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = child.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os_error = None
            try:
                import os
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError as exc:
                os_error = str(exc)
            child.wait()
            atomic_json(output / 'timeout.json', {'timeout': timeout, 'kill_race': os_error})
            code = -9
    copied = old.docker(['cp', container + ':' + remote_log + '/.', str(output)])
    atomic_json(output / 'runtime-provenance.json', {
        'schema': 'offline-safe-imports-v1', 'bundle_sha256': manifest['bundle_sha256'],
        'entry_sha256': manifest['resilient']['safe_entry_sha256'], 'image_id': info['Image'],
        'task': task, 'test_hashes': cfg['tests'], 'same_solver_filesystem': True,
        'new_image_layers': 0, 'network_isolation': 'new_network_namespace_at_grading_only'})
    if code:
        raise RuntimeError('offline_grading_process_exit:' + str(code))
    return old.validate_result(output, task)
