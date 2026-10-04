"""A bounded, provenance-pinned source-error replay; NOT a held-out benchmark.

Old successes are excluded before generation. Every eligible error gets at most
one solver attempt. Existing per-task pre-solve skills are retained. No replay
result is available while generating/fixing candidates. Fresh Harbor trials reuse
the original task images, model, agent, retriever, solver and verifier budgets.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from cross_chain_rsi import campaign as cross, admission, recovery, watchdog
from rsi_harness import clean_campaign as core, meta_skill_campaign as legacy
from rsi_harness.clean_rsi import SOLVE, digest, load_payload, public_trace, read_json
from rsi_harness.terminal_infra import atomic_json, file_hash, readiness
from . import learning

OLD = Path('/data2/ytzhang/rsi-harness-runs/cross-chain-20260929-qwen38-27b-v1')
DEFAULT = Path('/data2/ytzhang/rsi-harness-runs/skill-repair-v2-20260930')


def snapshot():
    return {**cross.code_snapshot(), **{str(p): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}}


def pinned_files(paths):
    return {str(p): file_hash(p) for p in paths if p.exists()}


def prepare(root):
    with legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return verify(root)
        m = cross.verify(OLD)
        source_manifest = read_json(cross.SOURCE_ROOT / 'manifest.json')
        rows, excluded = [], []
        for chain in m['source_order']:
            for task in m['source_tasks'][chain]:
                path = cross.record_path(cross.SOURCE_ROOT, chain, 'meta-skill-rsi', task)
                record = read_json(path)
                if record.get('status') != 'complete':
                    raise RuntimeError('incomplete_source:' + task)
                result = Path(record['result'])
                legacy.verify_task(cross.SOURCE_ROOT, source_manifest, task)
                issues = admission.compatibility(result, task, root)
                if issues:
                    excluded.append({'task': task, 'reason': 'invalid_source_environment_or_config', 'issues': issues})
                    continue
                if record['reward'] != 0:
                    excluded.append({'task': task, 'reason': 'already_passed', 'reward': record['reward']})
                    continue
                ok, reason = core.ready(task, cross.SOURCE_ROOT)
                if not ok:
                    raise RuntimeError('source_environment_not_ready:' + task + ':' + str(reason))
                start_path = result.parent / 'agent/rsi_start.json'
                start = read_json(start_path)
                if start['arm'] != 'skill-rsi' or start['version'] != record['before']:
                    raise RuntimeError('source_skill_version_mismatch')
                store = cross.SOURCE_ROOT / chain / 'meta-skill-rsi/state'
                payload = load_payload(store, start['version'])
                instruction = (core.DATASET / task / 'instruction.md').read_text()
                source = {'instruction': instruction, 'score': record['reward'],
                          'trace': public_trace(result.parent), 'current_skills': payload}
                entry = root / 'tasks' / task
                atomic_json(entry / 'source.json', source)
                paths = [path, result, start_path, result.parent / 'agent/trajectory.json',
                         result.parent / 'agent/rsi_events.jsonl', core.DATASET / task / 'instruction.md',
                         store / 'versions' / f"{start['version']['revision']:06d}" / 'payload.json']
                rows.append({'chain': chain, 'task': task, 'old_reward': record['reward'],
                    'old_result': str(result), 'source_sha256': file_hash(entry / 'source.json'),
                    'hashes': pinned_files(paths), 'before': start['version'],
                    'readiness': readiness(cross.SOURCE_ROOT, task)})
        manifest = {'schema': 'seen-error-repair-v2', 'created': time.time(), 'model': core.MODEL,
            'benchmark': 'Terminal-Bench-Evo', 'source_arm': 'meta-skill-rsi', 'rows': rows, 'excluded': excluded,
            'source_total': sum(len(v) for v in m['source_tasks'].values()), 'solve': SOLVE,
            'verifier_timeout': 600, 'learning': learning.BUDGET, 'ports': list(cross.PORTS),
            'retrieval': {'top_k': 4, 'chars': 12000}, 'replays_per_error': 1,
            'protocol': 'per-error incremental skill patch from its own public trace; all candidates freeze before replay; no replay-driven update',
            'comparison': 'historical failed source attempt vs fresh attempt with updated skill memory; no new baseline runs',
            'caveat': 'failure-selected seen-task development sanity check; NOT transfer evidence or causal isolation from resampling',
            'cross_manifest_sha256': file_hash(OLD / 'manifest.json')}
        atomic_json(root / 'manifest.json', manifest)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': snapshot()})
        return manifest


def verify(root):
    seal = read_json(root / 'seal.json')
    if not seal or seal['manifest_sha256'] != file_hash(root / 'manifest.json') or seal['code'] != snapshot():
        raise RuntimeError('pilot_protocol_or_code_changed')
    cross.verify(OLD)
    m = read_json(root / 'manifest.json')
    if m['cross_manifest_sha256'] != file_hash(OLD / 'manifest.json'):
        raise RuntimeError('original_manifest_changed')
    return m


def verify_row(root, row):
    if file_hash(root / 'tasks' / row['task'] / 'source.json') != row['source_sha256']:
        raise RuntimeError('pilot_source_changed')
    for p, h in row['hashes'].items():
        if file_hash(Path(p)) != h:
            raise RuntimeError('historical_source_changed:' + p)
    legacy.verify_task(cross.SOURCE_ROOT, read_json(cross.SOURCE_ROOT / 'manifest.json'), row['task'])


def output(root, job):
    return root / 'tasks' / job['task'] / job['phase'] / 'result.json'


def freeze_all(root, m):
    if (root / 'frozen.json').exists():
        frozen = read_json(root / 'frozen.json')
        for task, bank in frozen['banks'].items():
            learning.verify_bank(root / 'tasks' / task / 'learn', bank)
        return frozen
    banks, excluded = {}, []
    for row in m['rows']:
        task = row['task']
        folder = root / 'tasks' / task / 'learn'
        result = read_json(folder / 'result.json')
        if result is None:
            raise RuntimeError('freeze_before_all_learning_complete')
        if result['status'] == 'complete' and result['outcome'] == 'candidate_ready':
            banks[task] = learning.freeze_bank(folder)
        else:
            excluded.append({'task': task, 'reason': result.get('reason', result.get('outcome')),
                             'status': result['status']})
    frozen = {'time': time.time(), 'banks': banks, 'excluded': excluded,
              'manifest_sha256': file_hash(root / 'manifest.json')}
    atomic_json(root / 'frozen.json', frozen)
    return frozen


def execute_replay(root, row, port, frozen):
    task = row['task']
    folder = root / 'tasks' / task / 'replay'
    bank = root / 'tasks' / task / 'learn'
    expected = learning.verify_bank(bank, frozen['banks'][task])
    ok, reason = core.ready(task, cross.SOURCE_ROOT)
    if not ok or readiness(cross.SOURCE_ROOT, task)['image_id'] != row['readiness']['image_id']:
        raise RuntimeError('environment_not_ready_or_changed:' + str(reason))
    # Process-local parameterization; the sealed cross-chain module is NOT edited.
    previous_timeout = cross.VERIFIER_TIMEOUT
    try:
        cross.VERIFIER_TIMEOUT = 600
        record = cross.execute_trial(folder, core.DATASET / task, 'skill-rsi', bank / 'state', port, expected)
    finally:
        cross.VERIFIER_TIMEOUT = previous_timeout
    learning.verify_bank(bank, frozen['banks'][task])
    if record['selected_skills'] != read_json(bank / 'result.json')['selected_skills']:
        raise RuntimeError('retrieval_changed')
    issues = admission.compatibility(Path(record['result']), task, folder)
    if issues:
        raise RuntimeError('replay_config_incompatible:' + str(issues))
    record.update(old_reward=row['old_reward'], seen_task=True, target_learning=False)
    # Persist scored solve BEFORE the separate, non-authoritative qualitative audit.
    atomic_json(folder / 'scored.json', record)
    try:
        evidence = read_json(bank / 'evidence.json')['observations']
        check = learning.audit(folder, evidence, public_trace(Path(record['result']).parent),
                               core.MODEL, f'http://127.0.0.1:{port}/v1')
        atomic_json(folder / 'error_audit.json', check)
        record['audit_status'] = 'complete'
    except Exception as exc:
        record['audit_status'] = 'needs_review'
        record['audit_error'] = f'{type(exc).__name__}:{exc}'
    record['audit_accounting'] = learning.accounting(folder)
    return record


def worker(root, task, phase, port):
    m = verify(root)
    row = next(r for r in m['rows'] if r['task'] == task)
    if port not in m['ports'] or phase not in ('learn', 'replay'):
        raise ValueError('unregistered_work')
    job = {'task': task, 'phase': phase}
    path = output(root, job)
    with legacy.exclusive(path.parent / 'worker.lock'), legacy.exclusive(core.PROJECT / 'runs' / f'meta-port-{port}.lock'):
        atomic_json(path.parent / 'owner.json', {**job, 'port': str(port), 'pid': os.getpid(),
            'identity': core.process_identity(os.getpid()), 'started': time.time()})
        if path.exists():
            return
        try:
            verify_row(root, row)
            if phase == 'learn':
                result = learning.learn(path.parent, read_json(root / 'tasks' / task / 'source.json'),
                                        m['model'], f'http://127.0.0.1:{port}/v1')
            else:
                result = execute_replay(root, row, port, freeze_all(root, m))
            verify_row(root, row)
            atomic_json(path, result)
        except Exception as exc:
            atomic_json(path, {'status': 'blocked', 'task': task, 'phase': phase,
                'reason': f'{type(exc).__name__}:{exc}', 'time': time.time(),
                'accounting': learning.accounting(path.parent), 'automatic_retry': False})
            raise


def queue(root, m):
    pending = [{'task': r['task'], 'phase': 'learn'} for r in m['rows']
               if not output(root, {'task': r['task'], 'phase': 'learn'}).exists()]
    if pending:
        return pending
    frozen = freeze_all(root, m)
    return [{'task': r['task'], 'phase': 'replay'} for r in m['rows']
            if r['task'] in frozen['banks'] and not output(root, {'task': r['task'], 'phase': 'replay'}).exists()]


def report(root, m):
    rows = []
    for r in m['rows']:
        folder = root / 'tasks' / r['task']
        learn = read_json(folder / 'learn/result.json', {})
        replay = read_json(folder / 'replay/result.json', {})
        rows.append({'task': r['task'], 'old_reward': r['old_reward'], 'learn_status': learn.get('status', 'pending'),
            'skill_outcome': learn.get('outcome'), 'changed_ids': learn.get('changed_ids', []),
            'replay_status': replay.get('status', 'pending'), 'reward': replay.get('reward'),
            'reason': replay.get('reason', learn.get('reason')), 'audit_status': replay.get('audit_status')})
    completed = [r for r in rows if r['replay_status'] == 'complete']
    passed = sum(r['reward'] == 1 for r in completed)
    return {'time': time.time(), 'source_tasks': m['source_total'], 'errors': len(rows),
        'old_successes_not_replayed': sum(x['reason'] == 'already_passed' for x in m['excluded']),
        'learning_complete': sum(r['learn_status'] == 'complete' for r in rows),
        'candidates_ready': sum(r['skill_outcome'] == 'candidate_ready' for r in rows),
        'replays_complete': len(completed), 'repaired_tasks': passed,
        'repair_rate_among_scored_replays': passed / len(completed) if completed else None,
        'confirmed_repairs_over_all_original_errors': passed / len(rows) if rows else None,
        'blocked': [r for r in rows if 'blocked' in (r['learn_status'], r['replay_status'])],
        'rows': rows, 'interpretation': m['caveat']}


def check_handoff(root):
    note = OLD / 'PAUSE'
    saved = read_json(root / 'handoff.json')
    if not note.exists() or ('Owner: ' + str(root)) not in note.read_text():
        raise RuntimeError('old_campaign_not_paused_for_this_pilot')
    if saved is None:
        atomic_json(root / 'handoff.json', {'pause_sha256': file_hash(note), 'time': time.time()})
    elif file_hash(note) != saved['pause_sha256']:
        raise RuntimeError('pause_ownership_changed')


def resume_original(root):
    if read_json(root / 'handoff_resumed.json'):
        return
    cross.verify(OLD)
    if (OLD / 'PAUSE').exists():
        check_handoff(root)
        (OLD / 'PAUSE').rename(root / 'completed-handoff-PAUSE.txt')
    elif file_hash(root / 'completed-handoff-PAUSE.txt') != read_json(root / 'handoff.json')['pause_sha256']:
        raise RuntimeError('cannot_prove_handoff_release')
    atomic_json(root / 'handoff_released.json', {'time': time.time(), 'old': str(OLD)})
    launch = read_json(OLD / 'supervisor_launch.json')
    if not watchdog.owned_dispatcher(OLD, launch):
        watchdog.start(OLD)
    # The old watchdog exits on PAUSE; start a replacement only if its exact PID
    # is no longer live. Duplicate watchdogs fail closed on its existing flock.
    with (root / 'resumed-watchdog.log').open('ab', buffering=0) as log:
        child = subprocess.Popen([sys.executable, '-u', '-m', 'cross_chain_rsi.watchdog', '--root', str(OLD)],
            cwd=core.PROJECT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    atomic_json(root / 'handoff_resumed.json', {'time': time.time(), 'watchdog_pid': child.pid,
        'identity': core.process_identity(child.pid), 'supervisor': read_json(OLD / 'supervisor_launch.json')})


def adopt_workers(root, active):
    """Recover the Popen/active.json crash window without launching duplicates."""
    for path in (root / 'tasks').glob('*/*/owner.json'):
        job = read_json(path)
        if not legacy.alive(job) and not recovery.orphaned_child(path.parent):
            continue
        port = job['port']
        if port in active and active[port]['pid'] != job['pid'] and legacy.alive(active[port]):
            raise RuntimeError('multiple_workers_claim_port:' + port)
        active[port] = job
    return active


def run(root):
    m = verify(root)
    with legacy.exclusive(root / 'scheduler.lock'):
        prior_status = read_json(root / 'status.json', {})
        if prior_status.get('terminal') in ('complete', 'finished_with_blocked'):
            resume_original(root)
            return
        check_handoff(root)
        active = adopt_workers(root, read_json(root / 'active.json', {}))
        children, stopping = {}, False
        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            verify(root)
            check_handoff(root)
            for port, job in list(active.items()):
                if port in children:
                    children[port].poll()
                if legacy.alive(job) or recovery.orphaned_child(output(root, job).parent):
                    continue
                if not output(root, job).exists():
                    atomic_json(output(root, job), {'status': 'blocked', 'reason': 'worker_interrupted_no_automatic_retry', **job})
                del active[port]
            jobs = queue(root, m)
            disk = core.docker_disk_status(core.PROJECT)
            old_active = read_json(OLD / 'active.json', {})
            busy_old = {p for p, w in old_active.items() if legacy.alive(w)
                        or recovery.orphaned_child(cross.worker_output(OLD, w).parent)}
            if not stopping and not (root / 'PAUSE').exists() and disk['ok']:
                for port in map(str, m['ports']):
                    if port in active or port in busy_old or not core.endpoint_ok(port) or not legacy.endpoint_idle(port):
                        continue
                    job = next((j for j in jobs if not any((j['phase'], j['task']) == (w['phase'], w['task'])
                                                          for w in active.values())), None)
                    if not job:
                        break
                    # Check shared ownership too, not just momentary endpoint idleness.
                    try:
                        with legacy.exclusive(core.PROJECT / 'runs' / f'meta-port-{port}.lock'):
                            pass
                    except BlockingIOError:
                        continue
                    log_path = root / 'logs' / f"{job['phase']}-{job['task']}.log"
                    log_path.parent.mkdir(exist_ok=True)
                    with log_path.open('ab', buffering=0) as stream:
                        child = subprocess.Popen([sys.executable, '-u', '-m', 'skill_repair_v2.campaign',
                            '--root', str(root), '--worker', '--task', job['task'], '--phase', job['phase'], '--port', port],
                            cwd=core.PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                    children[port] = child
                    active[port] = {**job, 'pid': child.pid, 'identity': core.process_identity(child.pid), 'started': time.time()}
                    atomic_json(root / 'active.json', active)
            metrics = report(root, m)
            terminal = ('complete' if not metrics['blocked'] else 'finished_with_blocked') if not jobs and not active else None
            if (stopping or (root / 'PAUSE').exists()) and not active:
                terminal = 'paused'
            status = {'time': time.time(), 'pid': os.getpid(), 'identity': core.process_identity(os.getpid()),
                      'active': active, 'old_ports_draining': sorted(busy_old), 'disk': disk, 'terminal': terminal,
                      'phase': 'replay' if (root / 'frozen.json').exists() else 'learning'}
            atomic_json(root / 'active.json', active)
            atomic_json(root / 'status.json', status)
            atomic_json(root / 'metrics.json', metrics)
            monitor = root / 'monitor' / f'{int(time.time() // 1800)}.json'
            if not monitor.exists():
                atomic_json(monitor, {'status': status, 'metrics': metrics, 'note': 'diagnostic snapshot, NOT a Codex wakeup'})
            if terminal:
                if terminal != 'paused':
                    resume_original(root)
                return
            time.sleep(20)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=DEFAULT)
    g = p.add_mutually_exclusive_group(required=True)
    for flag in ('prepare', 'run', 'worker', 'report'):
        g.add_argument('--' + flag, action='store_true')
    p.add_argument('--task')
    p.add_argument('--phase', choices=('learn', 'replay'))
    p.add_argument('--port', type=int)
    a = p.parse_args()
    if a.prepare:
        m = prepare(a.root)
        print({'errors': len(m['rows']), 'excluded': m['excluded']})
    elif a.run:
        run(a.root)
    elif a.worker:
        worker(a.root, a.task, a.phase, a.port)
    else:
        print(report(a.root, verify(a.root)))


if __name__ == '__main__':
    main()
