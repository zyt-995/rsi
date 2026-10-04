"""Candidate-only non-regression protocol, with an audited v1 boundary handoff.

The original scored prefix is never replayed or relabelled as using new skills.
An already-scored boundary task may finish admission under v2 using its exact
saved candidate. Complete earlier v1 decisions remain unchanged historical data.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import threading
import time

from rsi_harness.clean_rsi import digest, read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi import bank, campaign as base, learning, runtime
from . import gate

core, legacy = base.core, base.legacy
PROTOCOL = 'self-skill-nonregression-v2'


def snapshot():
    return {**base.snapshot(), **{str(p): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}}


def verify(root):
    seal = read_json(root / 'seal.json', {})
    if seal.get('manifest_sha256') != file_hash(root / 'manifest.json') or seal.get('code') != snapshot():
        raise RuntimeError('frozen_protocol_or_code_changed')
    m = read_json(root / 'manifest.json')
    if m.get('schema') != PROTOCOL or m.get('gate') != gate.DEFAULT:
        raise RuntimeError('incorrect_nonregression_protocol')
    base.verify_files(m['migration']['frozen_source_files'])
    return m


def audit_native(entry, record, purpose, head=None):
    """Read/re-admit saved native artifacts; never launch an inference or grader."""
    gate.validate_record(entry, record, purpose, head)
    runtime.check_entry(entry)
    path = Path(record['result'])
    with runtime.ADMISSION_LOCK:
        issues = runtime.admission.compatibility(path, entry['task'], path.parent)
    data, start = read_json(path), read_json(path.parent / 'agent/rsi_start.json', {})
    if (issues or start.get('version') != record['before'] or start.get('arm') != 'skill-rsi'
            or start.get('selected_skills') != record.get('selected_skills')
            or data['verifier_result']['rewards']['reward'] != record['reward']):
        raise RuntimeError('saved_native_score_not_compatible:' + str(issues))


def check_source_stopped(root):
    if not (root / 'PAUSE').exists():
        raise RuntimeError('source_campaign_must_be_paused')
    for name in ('supervisor_launch.json', 'watchdog_launch.json'):
        if legacy.alive(read_json(root / name, {})):
            raise RuntimeError('source_controller_or_monitor_still_alive')
    for p in root.glob('stream/**/child.json'):
        if legacy.alive(read_json(p)):
            raise RuntimeError('source_trial_still_alive:' + str(p))


def migrate(root, source_root, auditor=audit_native, stopped=check_source_stopped):
    """No model/task calls. Preserve prefix + finish one frozen pending candidate."""
    root, source_root = root.resolve(), source_root.resolve()
    if root == source_root or source_root in root.parents or root in source_root.parents:
        raise ValueError('migration_requires_separate_run_directories')
    with legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            m = verify(root)
            if m['migration']['source_root'] != str(source_root):
                raise RuntimeError('migration_source_changed')
            return m
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('migration_target_not_empty; inspect_incomplete_preparation')
        stopped(source_root)
        old = base.verify(source_root)
        count = 0
        for e in old['stream']:
            if base.read_record(source_root / 'stream' / e['uid'] / 'record.json') is None:
                break
            count += 1
        if count >= len(old['stream']):
            raise RuntimeError('no_pending_boundary_update')
        completed = base.completed_prefix(source_root, old, count)
        entry = old['stream'][count]
        src = source_root / 'stream' / entry['uid']
        scored = read_json(src / 'scored.json')
        update = read_json(src / 'update/result.json')
        if not scored or not update or update.get('status') != 'candidate':
            raise RuntimeError('boundary_requires_scored_task_and_frozen_reviewed_candidate')
        before = completed[-1][1]['after'] if completed else read_json(source_root / 'bank/versions/000000/metadata.json')
        if read_json(source_root / 'bank/HEAD.json') != before or scored.get('before') != before:
            raise RuntimeError('cannot_rewrite_boundary_memory_history')
        auditor(entry, scored, 'stream', before)
        candidate_head = read_json(src / 'candidate/HEAD.json')
        payload, provenance = bank.load(src / 'candidate', candidate_head)
        if payload != update['payload'] or provenance != update['provenance']:
            raise RuntimeError('frozen_candidate_does_not_match_reviewed_proposal')
        if candidate_head != bank.metadata(payload, provenance, before['revision'] + 1, entry['uid']):
            raise RuntimeError('candidate_parent_or_origin_changed')
        cases = gate.select_cases(entry, completed)
        if read_json(src / 'validation_cases.json') != cases:
            raise RuntimeError('validation_cohort_changed_during_handoff')
        reused = {}
        for case in cases:
            p = src / 'validation' / case['uid'] / '00/candidate/record.json'
            r = read_json(p)
            if not r:
                raise RuntimeError('candidate_validation_not_complete; do_not_restart_it')
            auditor(case, r, 'validation', candidate_head)
            reused[case['uid']] = (p, r)
        # A later official attempt would already have used a different policy;
        # do not silently discard it or transplant an incompatible memory state.
        for e in old['stream'][count + 1:]:
            if (source_root / 'stream' / e['uid']).exists():
                raise RuntimeError('later_stream_work_exists; explicit_boundary_review_required')
        frozen = {str(source_root / name): file_hash(source_root / name) for name in ('manifest.json', 'seal.json')}
        for e, r in completed:
            auditor(e, read_json(source_root / 'stream' / e['uid'] / 'scored.json'), 'stream', r['before'])
        shutil.copytree(source_root / 'bank', root / 'bank')
        for e in old['stream'][:count + 1]:
            old_stage, new_stage = source_root / 'stream' / e['uid'], root / 'stream' / e['uid']
            for name in ('before.json', 'source.json', 'scored.json', 'validation_cases.json'):
                p = old_stage / name
                new_stage.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, new_stage / name)
                frozen[str(p)] = file_hash(p)
                frozen[str(new_stage / name)] = file_hash(new_stage / name)
            for name in ('update', 'parent', 'candidate'):
                p = old_stage / name
                if p.exists():
                    shutil.copytree(p, new_stage / name)
                    for f in p.rglob('*'):
                        if f.is_file():
                            frozen[str(f)] = file_hash(f)
                            cloned = new_stage / name / f.relative_to(p)
                            frozen[str(cloned)] = file_hash(cloned)
            if e['uid'] != entry['uid']:
                p = old_stage / 'record.json'
                shutil.copy2(p, new_stage / 'record.json')
                frozen[str(p)] = file_hash(p)
                frozen[str(new_stage / 'record.json')] = file_hash(new_stage / 'record.json')
        for uid, (p, r) in reused.items():
            atomic_json(root / 'stream' / entry['uid'] / 'validation' / uid / '00/candidate/record.json', r)
            frozen[str(p)] = file_hash(p)
        m = copy.deepcopy(old)
        m.update(schema=PROTOCOL, created=time.time(), gate=gate.DEFAULT,
            protocol='score once; reuse immutable official scores; candidate-only regression checks; ties accepted after evidence review; persistent cross-chain memory',
            claims='development protocol transition, not a homogeneous preregistered v2 run; non-regression admission does not prove benefit, causality or transfer',
            migration={'source_root': str(source_root), 'completed_v1_steps': count,
                'boundary_update_uid': entry['uid'], 'official_prefix_scored_under_v1': count + 1,
                'first_new_protocol_solve_index': count + 1, 'first_new_protocol_solve_uid': old['stream'][count + 1]['uid'] if count + 1 < len(old['stream']) else None,
                'reused_candidate_validation_uids': list(reused), 'frozen_source_files': frozen,
                'reason': 'user requested tie admission and elimination of redundant parent replays'})
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': snapshot()})
        atomic_json(root / 'status.json', {'time': time.time(), 'state': 'prepared_not_running',
                                          'completed': count, 'no_new_native_solves': True})
        return m


def pinned(path, value):
    old = read_json(path)
    if old is not None and old != value:
        raise RuntimeError('immutable_input_changed:' + str(path))
    atomic_json(path, value)


def step(root, m, index, solve=runtime.native_solve, proposer=learning.propose, checker=runtime.check_entry):
    entry = m['stream'][index]
    folder = root / 'stream' / entry['uid']
    prior = base.read_record(folder / 'record.json')
    if prior:
        if prior.get('status') != 'complete' or prior.get('uid') != entry['uid']:
            raise RuntimeError('invalid_completed_stage_record')
        base.verify_files(prior.get('artifact_hashes', {}))
        return prior
    if (folder / 'blocked.json').exists():
        raise RuntimeError('stage_quarantined; no_automatic_retry')
    completed = base.completed_prefix(root, m, index)
    before = completed[-1][1]['after'] if completed else read_json(root / 'bank/versions/000000/metadata.json')
    intent = read_json(folder / 'commit_intent.json')
    if read_json(root / 'bank/HEAD.json') not in [before] + ([intent['after']] if intent else []):
        raise RuntimeError('global_bank_not_at_registered_stream_position')
    checker(entry)
    pinned(folder / 'before.json', before)
    old, provenance = bank.load(root / 'bank', before)
    parent = folder / 'parent'
    bank.publish(parent, old, provenance, before['revision'], before['after_task'])
    scored = read_json(folder / 'scored.json')
    if scored is None:
        scored = solve(entry, parent, before, folder / 'solve', m['ports'][0], 'stream')
    gate.validate_record(entry, scored, 'stream', before)
    pinned(folder / 'scored.json', scored)
    source = learning.source_from_record(entry, scored)
    pinned(folder / 'source.json', source)
    cases = gate.select_cases(entry, completed)
    pinned(folder / 'validation_cases.json', cases)
    references = {entry['uid']: scored}
    prior_scores = {e['uid']: read_json(root / 'stream' / e['uid'] / 'scored.json') for e, _ in completed}
    references.update({e['uid']: prior_scores[e['uid']] for e in cases[1:]})
    pinned(folder / 'validation_references.json', references)
    for e in cases:
        checker(e)
    update = proposer(folder / 'update', source, old, provenance, base.learning_history(root, completed),
                      m['model'], f"http://127.0.0.1:{m['ports'][0]}/v1")
    decision = {'accept': False, 'reason': 'no_evidence_reviewed_change', 'comparisons': []}
    after = before
    if update['status'] == 'candidate':
        candidate = folder / 'candidate'
        candidate_head = bank.publish(candidate, update['payload'], update['provenance'], before['revision'] + 1, entry['uid'])
        decision = gate.evaluate(folder / 'validation', cases, references, parent, before,
                                 candidate, candidate_head, m['ports'], solve)
        if decision['accept']:
            planned = {'before': before, 'after': candidate_head, 'candidate_sha256': digest(update['payload']),
                       'validation_sha256': file_hash(folder / 'validation/result.json'), 'protocol': PROTOCOL}
            pinned(folder / 'commit_intent.json', planned)
            after = bank.promote(root / 'bank', before, update['payload'], update['provenance'], entry['uid'])
    if read_json(root / 'bank/HEAD.json') != after:
        raise RuntimeError('bank_changed_before_stage_finalization')
    artifacts = dict(scored['hashes'])
    for p in folder.glob('*.json'):
        if p.name not in ('record.json', 'blocked.json'):
            artifacts[str(p)] = file_hash(p)
    for sub in ('update', 'validation', 'parent', 'candidate'):
        for p in (folder / sub).rglob('*.json'):
            if '/trials/' not in str(p):
                artifacts[str(p)] = file_hash(p)
    for row in decision['comparisons']:
        artifacts.update(row['reference']['hashes'])
        artifacts.update(row['candidate']['hashes'])
    historical = index < m.get('migration', {}).get('official_prefix_scored_under_v1', 0)
    result = {**scored, 'chain': entry['chain'], 'version': entry['version'], 'after': after,
              'solve_protocol': 'cross-chain-self-skill-v1' if historical else PROTOCOL,
              'update_protocol': PROTOCOL, 'historical_formal_score_reused': historical,
              'update_status': update['status'], 'promoted': decision['accept'], 'promotion_reason': decision['reason'],
              'effectiveness_proven': False, 'learning_usage': update.get('accounting', {}),
              'validation_pairs': 0, 'candidate_validations': len(decision['comparisons']),
              'artifact_hashes': artifacts}
    base.save_record(folder / 'record.json', result)
    return base.read_record(folder / 'record.json')


def report(root, m):
    r = base.report(root, m)
    prefix = m['migration']['official_prefix_scored_under_v1']
    groups = {}
    for label, entries in [('historical_v1_formal_scores', m['stream'][:prefix]),
                           ('v2_formal_scores', m['stream'][prefix:])]:
        values = [read_json(root / 'stream' / e['uid'] / 'scored.json') for e in entries]
        values = [v for v in values if v]
        groups[label] = {'count': len(values), 'passed': sum(v['reward'] for v in values),
                         'mean_reward': sum(v['reward'] for v in values) / len(values) if values else None}
    payload, _ = bank.load(root / 'bank')
    old_root = Path(m['migration']['source_root'])
    old_validation = [read_json(p) for p in old_root.glob('stream/*/validation/*/*/*/record.json')]
    new_validation = [read_json(p) for p in root.glob('stream/*/validation/*/*/*/record.json')]
    unique = {v['result']: v for v in old_validation + new_validation}
    return {**r, 'protocol': PROTOCOL, 'score_cohorts': groups, 'active_skill_count': len(payload),
            'active_skill_ids': sorted(payload), 'parent_replays_in_v2': 0,
            'validation_total_including_legacy_deduplicated': len(unique),
            'validation_seconds_including_legacy_deduplicated': sum(v.get('solve_seconds', 0) for v in unique.values()),
            'cancelled_legacy_parent_replays': len(list(old_root.glob('stream/*/validation/*/*/parent/cancelled_by_protocol_change.json')))}


def run(root, settle_only=False):
    with legacy.exclusive(root / 'scheduler.lock'):
        m = verify(root)
        check_source_stopped(Path(m['migration']['source_root']))
        with runtime.inference_lease(m['ports']):
            if (root / 'PAUSE').exists():
                raise RuntimeError('campaign_paused')
            for p in root.glob('stream/**/child.json'):
                if legacy.alive(read_json(p)):
                    raise RuntimeError('orphan_native_trial_still_alive')
            stop_heartbeat = threading.Event()
            heartbeat = threading.Thread(target=base.heartbeat, args=(root, stop_heartbeat), daemon=True)
            heartbeat.start()
            stopping = False
            def stop(_sig, _frame):
                nonlocal stopping
                stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            try:
                for index, entry in enumerate(m['stream']):
                    verify(root)
                    if stopping or (root / 'PAUSE').exists() or (settle_only and index >= m['migration']['first_new_protocol_solve_index']):
                        atomic_json(root / 'status.json', {'time': time.time(), 'state': 'paused', 'completed': index})
                        return
                    status = {'time': time.time(), 'pid': os.getpid(), 'identity': core.process_identity(os.getpid()),
                              'state': 'running', 'uid': entry['uid'], 'task': entry['task'], 'completed': index,
                              'protocol': PROTOCOL}
                    atomic_json(root / 'status.json', status)
                    try:
                        step(root, m, index)
                    except Exception as exc:
                        blocked = {**status, 'state': 'blocked', 'reason': f'{type(exc).__name__}:{exc}',
                                   'automatic_retry': False, 'score_is_not_overwritten': True}
                        atomic_json(root / 'stream' / entry['uid'] / 'blocked.json', blocked)
                        atomic_json(root / 'status.json', blocked)
                        raise
                    finally:
                        atomic_json(root / 'metrics.json', report(root, m))
                atomic_json(root / 'status.json', {'time': time.time(), 'state': 'complete', 'completed': len(m['stream'])})
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--migrate-from', type=Path)
    actions.add_argument('--run', action='store_true')
    actions.add_argument('--report', action='store_true')
    actions.add_argument('--settle-only', action='store_true', help='Finish only the imported boundary update; no new stream task.')
    parser.add_argument('--allow-native-runs', action='store_true')
    args = parser.parse_args()
    if args.migrate_from:
        m = migrate(args.root, args.migrate_from)
        print(json.dumps({'migrated': True, 'preserved_scores': m['migration']['official_prefix_scored_under_v1'],
                          'new_native_runs': 0, 'root': str(args.root)}))
    elif args.report:
        print(json.dumps(report(args.root, verify(args.root)), ensure_ascii=False, indent=2))
    elif not args.allow_native_runs:
        parser.error('--run/--settle-only require --allow-native-runs')
    else:
        run(args.root, settle_only=args.settle_only)


if __name__ == '__main__':
    main()
