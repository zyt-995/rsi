"""Host-side adapter: inject only after solving, grade in the SAME filesystem."""
import json
import io
import os
from pathlib import Path
import signal
import subprocess
import tarfile

from .profiles import PREFIX, SCHEMA, sha, test_hashes


def load_manifest(path, expected_hash):
    path = Path(path)
    if sha(path) != expected_hash:
        raise RuntimeError('offline_manifest_hash_mismatch')
    value = json.loads(path.read_text())
    if value['schema'] != SCHEMA or sha(value['bundle']) != value['bundle_sha256']:
        raise RuntimeError('offline_bundle_changed')
    for filename, expected in value['code'].items():
        if sha(filename) != expected:
            raise RuntimeError('offline_verifier_code_changed:' + filename)
    return value


def docker(args, timeout=120):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('docker_operation_failed:' + result.stderr[-3000:])
    return result


def upload_tests(container, source):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for path in sorted(source.rglob('*')):
            if '__pycache__' in path.parts:
                continue
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(source)))
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            if path.is_file():
                with path.open('rb') as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
    subprocess.run(['docker', 'exec', '-i', container, 'tar', '--no-same-owner', '-xf', '-', '-C', '/tests'],
                   input=archive.getvalue(), check=True, capture_output=True, timeout=120)


def host_pid(namespace_pid, proc=Path('/proc')):
    """Resolve Docker's rootless PID to a host PID; ambiguity always fails closed."""
    candidates = []
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != os.getuid():
                continue
            row = next(x for x in (p / 'status').read_text().splitlines() if x.startswith('NSpid:'))
            ids = [int(x) for x in row.split()[1:]]
            # rootlesskit: host / daemon / container; rootful: host / container.
            if len(ids) >= 2 and ids[-2:] == [namespace_pid, 1]:
                candidates.append(int(p.name))
        except (OSError, StopIteration, ValueError):
            continue
    if len(candidates) != 1:
        raise RuntimeError('cannot_uniquely_resolve_container_pid:' + str(candidates))
    return candidates[0]


def validate_result(output, task):
    error = output / 'offline-error.json'
    if error.exists():
        raise RuntimeError('offline_grading_infrastructure_error:' + error.read_text())
    record = json.loads((output / 'offline-result.json').read_text())
    network = json.loads((output / 'network.json').read_text())
    report = json.loads((output / 'ctrf.json').read_text())
    summary = report['results']['summary']
    tests = report['results']['tests']
    if (record.get('status') != 'complete' or record.get('task') != task
            or network.get('network') != 'isolated_loopback_only'
            or network.get('loopback') != 'passed' or not network.get('capabilities_dropped')):
        raise RuntimeError('offline_grader_attestation_missing')
    if (type(summary.get('tests')) is not int or summary['tests'] <= 0
            or len(tests) != summary['tests'] or any(t.get('status') not in ('passed', 'failed') for t in tests)
            or summary.get('passed', 0) + summary.get('failed', 0) != summary['tests']
            or sum(t.get('status') == 'passed' for t in tests) != summary.get('passed', 0)
            or sum(t.get('status') == 'failed' for t in tests) != summary.get('failed', 0)
            or any(summary.get(k, 0) for k in ('other', 'pending', 'skipped'))):
        raise RuntimeError('invalid_offline_test_report')
    reward = int(summary.get('failed', 0) == 0)
    if record.get('reward') != reward or (output / 'reward.txt').read_text().strip() != str(reward):
        raise RuntimeError('offline_reward_report_disagreement')
    return record


def grade_container(container, task, manifest, output, timeout=590):
    """No model calls, container commits, image builds, or network downloads."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / n).exists() for n in ('offline-result.json', 'offline-error.json', 'network.json')):
        raise RuntimeError('grading_output_already_exists; never_overwrite_an_attempt')
    cfg = manifest['tasks'][task]
    if test_hashes(Path(cfg['path'])) != cfg['tests']:
        raise RuntimeError('registered_official_tests_changed')
    info = json.loads(docker(['inspect', container]).stdout)[0]
    if info['Image'] != cfg['image_id'] or not info['State']['Running']:
        raise RuntimeError('wrong_or_stopped_task_image')
    # No runtime or hidden tests are installed until this grading-only entrypoint.
    # Single-UID rootless Docker cannot chown host UID 1007 via docker cp.
    # Stream a normalized archive instead; no duplicate on-disk upload is kept.
    with Path(manifest['bundle']).open('rb') as archive:
        subprocess.run(['docker', 'exec', '-i', container, 'tar', '--no-same-owner', '-xzf', '-', '-C', '/'],
                       stdin=archive, capture_output=True, check=True, timeout=120)
    docker(['exec', container, 'mkdir', '-p', '/tests', '/logs/verifier'])
    upload_tests(container, Path(cfg['path']) / 'tests')
    pid = host_pid(info['State']['Pid'])
    # Namespace handles refer to this exact live container (not just a matching PID).
    if Path(f'/proc/{pid}/root/etc/hostname').read_text().strip() != info['Config']['Hostname']:
        raise RuntimeError('container_pid_identity_mismatch')
    command = ['nsenter', '--preserve-credentials', '--target', str(pid), '--user', '--mount',
               '--pid', '--root', '--wd', '--', PREFIX + '/python/bin/python3', PREFIX + '/isolated.py', task]
    failure = None
    with (output / 'test-stdout.txt').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            if code:
                failure = RuntimeError('offline_grading_process_exit:' + str(code))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            failure = RuntimeError('offline_grading_timeout_not_zero')
    # Docker copy merges with the host log. The in-container stdout file is not used.
    docker(['cp', container + ':/logs/verifier/.', str(output)])
    (output / 'runtime-provenance.json').write_text(json.dumps({
        'schema': SCHEMA, 'bundle_sha256': manifest['bundle_sha256'],
        'image_id': info['Image'], 'task': task, 'test_hashes': cfg['tests'],
        'network_isolation': 'new_network_namespace_at_grading_only',
        'same_solver_filesystem': True, 'new_image_layers': 0}, indent=2))
    if failure:
        raise failure
    return validate_result(output, task)
