"""Resume checkpoints, regrade retained files, never search by replaying zeros."""
import copy
import json
from pathlib import Path
import shutil
import os
import signal
import subprocess
import time

from offline_verifier.runner import docker, validate_result
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi import learning, runtime as original
from self_skill_rsi_v2 import gate
from . import grading


def wait_child(folder):
    child = read_json(folder / 'child.json', {})
    started = read_json(folder / 'launch.json', {}).get('started', time.time())
    while original.legacy.alive(child):
        if time.time() - started > 10920:
            raise RuntimeError('orphan_trial_exceeded_budget; retained_for_inspection')
        time.sleep(5)


def signal_owned_process(record, allowed_module, sig):
    """PID reuse, user and executable checks before every individual signal."""
    if not original.legacy.alive(record):
        return False
    p = Path('/proc') / str(record['pid'])
    argv = p.joinpath('cmdline').read_bytes().decode().split('\0')
    if p.stat().st_uid != os.getuid() or allowed_module not in argv:
        raise RuntimeError('refuse_to_signal_unregistered_process')
    if original.core.process_identity(record['pid']) != record['identity']:
        return False
    os.kill(record['pid'], sig)
    return True


def grading_only_failure(result):
    exc = result.get('exception_info') or {}
    return (not result.get('verifier_result')
            and bool((result.get('agent_execution') or {}).get('finished_at'))
            and bool((result.get('verifier') or {}).get('started_at'))
            and (str(exc.get('exception_message', '')).startswith('offline_grading_')
                 or exc.get('exception_type') in ('VerifierTimeoutError', 'VerifierOutputParseError')))


def retained(result_path):
    value = read_json(result_path.parent / 'verifier/retained-container.json')
    if not value:
        raise RuntimeError('failed_trial_has_no_retained_container; no_blind_replay')
    info = grading.inspect_container(value['container'], value['image'])
    return value, info


def passive_entrypoint(cmd):
    return cmd in (['sleep', 'infinity'], ['tail', '-f', '/dev/null'],
                   ['/bin/sh', '-c', 'tail -f /dev/null'], ['sh', '-c', 'tail -f /dev/null'],
                   ['bash', '-c', 'tail -f /dev/null'], ['sh', '-c', 'sleep infinity'],
                   ['/bin/sh', '-c', 'sleep infinity'], ['bash', '-c', 'sleep infinity'])


def regrade(entry, store, head, attempt, path, port, purpose, manifest):
    """One additional grader attempt on the same filesystem, zero model calls."""
    dest = attempt / 'regrade'
    cached = read_json(dest / 'record.json')
    if cached:
        gate.validate_record(entry, cached, purpose, head)
        return cached
    derived = dest / 'trials' / path.parent.name
    if (derived / 'result.json').exists():
        return original.native_solve(entry, store, head, dest, port, purpose)
    started = read_json(dest / 'started.json')
    if started:
        if started['source_sha256'] != file_hash(path):
            raise RuntimeError('regrade_source_changed')
        # Crash after the grade finished: use its completed evidence, not another grade.
        record = validate_result(derived / 'verifier', entry['task'])
        value = read_json(path.parent / 'verifier/retained-container.json')
    else:
        value, info = retained(path)
        if not info['State']['Running']:
            # Restart only passive keepalive entrypoints; never rerun arbitrary task init.
            cmd = (info['Config'].get('Entrypoint') or []) + (info['Config'].get('Cmd') or [])
            if not passive_entrypoint(cmd):
                raise RuntimeError('unsafe_retained_container_restart:' + repr(cmd))
            docker(['start', value['container']])
        atomic_json(dest / 'started.json', {'time': time.time(), 'source': str(path),
            'source_sha256': file_hash(path), 'same_container': value['container'], 'model_calls': 0})
        try:
            record = grading.grade_container(value['container'], entry['task'], manifest, derived / 'verifier')
        finally:
            docker(['stop', '--time', '5', value['container']])
    for src in (path.parent / 'agent').rglob('*'):
        if src.is_file():
            dst = derived / 'agent' / src.relative_to(path.parent / 'agent')
            if dst.exists() and file_hash(dst) != file_hash(src):
                raise RuntimeError('regrade_agent_artifact_changed')
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    (derived / 'agent').mkdir(exist_ok=True)
    for name in ('config.json', 'lock.json'):
        if (path.parent / name).exists():
            shutil.copy2(path.parent / name, derived / name)
    atomic_json(derived / 'verifier/retained-container.json', value)
    data = copy.deepcopy(read_json(path))
    data['exception_info'] = None
    data['verifier_result'] = {'rewards': {'reward': record['reward']}}
    data['recovery_provenance'] = {'raw_result': str(path), 'raw_sha256': file_hash(path),
        'same_solver_filesystem': True, 'new_model_calls': 0, 'extra_grader_attempts': 1}
    atomic_json(derived / 'result.json', data)
    shutil.copy2(attempt / 'launch.json', dest / 'launch.json')
    return original.native_solve(entry, store, head, dest, port, purpose)


def clean_success(record):
    """Remove only this admitted trial's stopped disposable container."""
    path = Path(record['result'])
    value = read_json(path.parent / 'verifier/retained-container.json')
    if not value:
        return
    receipt = path.parent / 'container-cleanup.json'
    if receipt.exists():
        return
    try:
        info = grading.inspect_container(value['container'], value['image'])
        if info['State']['Running']:
            raise RuntimeError('valid_trial_container_still_running')
        docker(['rm', value['container']])
        atomic_json(receipt, {'container': value['container'], 'removed': True, 'time': time.time(),
                             'reason': 'valid_score_and_trajectory_persisted'})
    except Exception as exc:
        # Cleanup is not a scientific failure; never rerun the model for it.
        atomic_json(receipt, {'removed': False, 'error': str(exc), 'time': time.time()})


def native_solve(entry, store, head, folder, port, purpose, *, manifest):
    folder = Path(folder)
    with original.legacy.exclusive(folder / 'solve.lock'):
        cached = read_json(folder / 'record.json')
        if cached:
            gate.validate_record(entry, cached, purpose, head)
            clean_success(cached)
            return cached
        attempt = folder / 'attempts/00'
        wait_child(attempt)
        try:
            result = original.native_solve(entry, store, head, attempt, port, purpose)
        except Exception as exc:
            paths = list((attempt / 'trials').glob('*/result.json'))
            if len(paths) != 1 or not grading_only_failure(read_json(paths[0])):
                atomic_json(folder / 'failure.json', {'time': time.time(), 'reason': str(exc),
                    'score': None, 'automatic_model_replay': False})
                raise
            result = regrade(entry, store, head, attempt, paths[0], port, purpose, manifest)
        atomic_json(folder / 'record.json', result)
        clean_success(result)
        return result


def propose(folder, source, old, provenance, history, model, url):
    """Invalid or interrupted proposals retain the old bank, without new sampling."""
    if (folder / 'fallback.json').exists():
        value = read_json(folder / 'result.json')
        if value and value.get('status') == 'no_change' and value.get('payload') == old:
            return value
        raise RuntimeError('invalid_saved_learning_fallback')
    try:
        return learning.propose(folder, source, old, provenance, history, model, url)
    except Exception as exc:
        # Integrity problems are never converted into no-ops.
        if isinstance(exc, RuntimeError) and not str(exc).startswith('interrupted_request_not_repeated:'):
            raise
        if not isinstance(exc, (ValueError, TimeoutError, OSError, RuntimeError)):
            raise
        if (folder / 'result.json').exists():
            raise
        reason = type(exc).__name__ + ':' + str(exc)
        result = {'status': 'no_change', 'payload': old, 'provenance': provenance,
            'diagnosis': read_json(folder / 'diagnosis.json'), 'accounting': learning.accounting(folder),
            'proposal_failure': reason, 'failure_policy': 'retain_old_bank_without_resampling',
            'effectiveness_proven': False}
        atomic_json(folder / 'result.json', result)
        atomic_json(folder / 'fallback.json', {'time': time.time(), 'reason': reason,
            'model_retries': 0, 'score_unchanged': True})
        return result
