"""Audited continuation of eight completed scores, with resilient execution."""
import argparse
import copy
from contextlib import contextmanager
from functools import partial
import json
import os
from pathlib import Path
import signal
import threading
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_accel import campaign as prior
from . import grading, runtime

base, v2 = prior.base, prior.v2
POLICY = {'model_replays_of_valid_scores': 0, 'same_filesystem_extra_grades': 1,
          'controller_crash_restarts': 3, 'controller_backoff_seconds': [30, 120, 300],
          'invalid_update': 'retain_old_bank_without_resampling',
          'unknown_integrity_failure': 'needs_attention', 'idle_release_seconds': 300,
          'guardian_poll_seconds': 20, 'half_hour_snapshots': True}


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def prepare(root, source, grader, acceptance):
    root, source = root.resolve(), source.resolve()
    if root == source or source in root.parents or root in source.parents:
        raise ValueError('separate_migration_directory_required')
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('migration_target_not_empty')
        prior.offline.stopped(source)
        old = prior.verify(source)
        count = 0
        for e in old['stream']:
            if not (source / 'stream' / e['uid'] / 'record.json').exists():
                break
            count += 1
        completed = base.completed_prefix(source, old, count)
        if not completed or read_json(source / 'bank/HEAD.json') != completed[-1][1]['after']:
            raise RuntimeError('not_a_committed_prefix')
        pending = source / 'stream' / old['stream'][count]['uid']
        failed = list((pending / 'solve/trials').glob('*/result.json'))
        if ((pending / 'scored.json').exists() or len(failed) != 1
                or not runtime.grading_only_failure(read_json(failed[0]))):
            raise RuntimeError('only_the_reviewed_unscored_grader_failure_may_be_replaced')
        for e in old['stream'][count+1:]:
            if (source / 'stream' / e['uid']).exists():
                raise RuntimeError('later_work_would_be_discarded')
        checks = read_json(acceptance)
        if not checks or checks.get('passed') is not True or checks.get('grader_sha256') != file_hash(grader):
            raise RuntimeError('acceptance_not_passed_for_this_grader')
        grading.load_manifest(grader, file_hash(grader))
        frozen = prior.clone_prefix(source, root, completed)
        for p in list(pending.rglob('*')) + [source / 'manifest.json', source / 'seal.json', grader, acceptance]:
            if p.is_file():
                frozen[str(p)] = file_hash(p)
        frozen.update(checks.get('evidence_hashes', {}))
        atomic_json(source / 'PAUSE', {'time': time.time(), 'reason': 'resilient infrastructure migration', 'target': str(root)})
        m = copy.deepcopy(old)
        m['resilience_migration'] = {'source_root': str(source), 'created': time.time(),
            'preserved_official_uids': [e['uid'] for e, _ in completed],
            'superseded_invalid_result': str(failed[0]), 'superseded_invalid_sha256': file_hash(failed[0]),
            'explicit_replacement_uid': old['stream'][count]['uid'],
            'replacement_reason': 'old invalid grader attempt lost its container; user authorized repair; no valid score exists',
            'grader': str(grader.resolve()), 'grader_sha256': file_hash(grader),
            'acceptance': str(acceptance.resolve()), 'implementation': code(),
            'frozen_files': frozen, 'policy': POLICY,
            'interpretation': 'execution infrastructure transition; prior valid scores unchanged; '
                              'invalid learning proposals now retain prior skills rather than halt the stream'}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root / 'status.json', {'time': time.time(), 'state': 'prepared_not_running', 'completed': count})
        return verify(root)


def verify(root):
    m = prior.offline.verify(root)
    change = m['resilience_migration']
    if change['implementation'] != code() or change['policy'] != POLICY:
        raise RuntimeError('resilience_implementation_or_policy_changed')
    source = Path(change['source_root'])
    v2.check_source_stopped(source)
    old = prior.verify(source)
    if {k:v for k,v in m.items() if k != 'resilience_migration'} != old:
        raise RuntimeError('unregistered_experiment_change')
    base.verify_files(change['frozen_files'])
    grading.load_manifest(change['grader'], change['grader_sha256'])
    return m


def report(root, m):
    return {**prior.report(root, m), 'resilience': m['resilience_migration']['interpretation'],
            'learning_fallbacks': [str(p) for p in root.glob('stream/*/update/fallback.json')],
            'regrades': [str(p) for p in root.glob('stream/**/regrade/started.json')]}


@contextmanager
def adapter(m):
    change = m['resilience_migration']
    old_command = base.core.harbor_command
    def command(*args, **kwargs):
        return prior.offline.ORIGINAL_COMMAND(*args, **kwargs) + [
            '--verifier', 'self_skill_rsi_resilient.harbor:ResilientVerifier',
            '--verifier-kwarg', 'manifest=' + change['grader'],
            '--verifier-kwarg', 'manifest_sha256=' + change['grader_sha256'],
            '--environment-kwarg', 'keep_containers=true']
    base.core.harbor_command = command
    try:
        yield
    finally:
        base.core.harbor_command = old_command


def run(root):
    with base.legacy.exclusive(root / 'scheduler.lock'):
        m = verify(root)
        # A restarted controller attaches to outstanding children before leasing.
        for p in root.glob('stream/**/child.json'):
            runtime.wait_child(p.parent)
        with prior.offline.runtime.inference_lease(m['ports']), adapter(m):
            stopping = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopping.set())
            signal.signal(signal.SIGINT, lambda *_: stopping.set())
            heart = threading.Thread(target=base.heartbeat, args=(root, stopping), daemon=True)
            heart.start()
            change = m['resilience_migration']
            native = partial(runtime.native_solve, manifest=grading.load_manifest(change['grader'], change['grader_sha256']))
            try:
                for index, entry in enumerate(m['stream']):
                    if (root / 'stream' / entry['uid'] / 'record.json').exists():
                        continue
                    if stopping.is_set() or (root / 'PAUSE').exists():
                        atomic_json(root / 'status.json', {'state': 'paused', 'time': time.time(), 'completed': index})
                        return
                    verify(root)
                    status = {'time': time.time(), 'stage_started': time.time(), 'state': 'running',
                        'pid': os.getpid(), 'identity': base.core.process_identity(os.getpid()),
                        'uid': entry['uid'], 'task': entry['task'], 'completed': index}
                    atomic_json(root / 'status.json', status)
                    try:
                        v2.step(root, m, index, solve=native, proposer=runtime.propose)
                    except Exception as exc:
                        atomic_json(root / 'status.json', {**status, 'time': time.time(), 'state': 'needs_attention',
                            'reason': type(exc).__name__ + ':' + str(exc), 'invalid_is_not_zero': True,
                            'automatic_model_replay': False})
                        return
                    finally:
                        atomic_json(root / 'metrics.json', report(root, m))
                atomic_json(root / 'status.json', {'state': 'complete', 'time': time.time(), 'completed': len(m['stream'])})
            finally:
                stopping.set()
                heart.join(timeout=5)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--prepare-from', type=Path)
    p.add_argument('--grader', type=Path)
    p.add_argument('--acceptance', type=Path)
    p.add_argument('--run', action='store_true')
    p.add_argument('--report', action='store_true')
    p.add_argument('--allow-native-runs', action='store_true')
    a = p.parse_args()
    if a.prepare_from:
        prepare(a.root, a.prepare_from, a.grader, a.acceptance)
    elif a.report:
        print(json.dumps(report(a.root, verify(a.root)), indent=2))
    elif a.run and a.allow_native_runs:
        run(a.root)
    else:
        p.error('explicit action required')
