"""Sequential cross-chain Self-Skill RSI with parallel historical validation.

Usage (from the repository, after sourcing scripts/docker-env.sh):
  python -m self_skill_rsi.campaign --prepare --config examples/self-skill-rsi-development.json --root RUN
  python -m self_skill_rsi.campaign --report --root RUN
  python -m self_skill_rsi.campaign --run --allow-native-runs --root RUN

Preparation never calls a model or replays a task. Run is explicit and refuses
live older dispatchers instead of stealing their endpoints. One persistent bank
is used across ALL chain boundaries. Different methods must have independent
banks and the same registered order; prior chain-reset controls are not equivalent.
"""
import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path

from rsi_harness import clean_campaign as core, meta_skill_campaign as legacy
from rsi_harness.clean_rsi import SOLVE, digest, read_json
from rsi_harness.terminal_infra import atomic_json, file_hash, fingerprint, readiness
from cross_chain_rsi import campaign as cross
from . import bank, gate, learning, runtime


def snapshot():
    project = core.PROJECT
    paths = sorted((project / 'self_skill_rsi').glob('*.py')) + sorted((project / 'skill_repair_v2').glob('*.py'))
    return {**cross.code_snapshot(), **{str(p): file_hash(p) for p in paths}}


def prepare(root, config_path):
    with legacy.exclusive(root / 'scheduler.lock'):
        if (root / 'manifest.json').exists():
            m = verify(root)
            if read_json(config_path) != m['request']:
                raise RuntimeError('prepared_configuration_changed')
            return m
        config = read_json(config_path)
        required = {'schema', 'chains', 'heldout_chains', 'ports', 'readiness_root', 'cohort_role', 'validation_repeats'}
        if set(config) != required or config['schema'] != 'self-skill-rsi-config-v1':
            raise ValueError('invalid_configuration_schema')
        chain_ids, probes = config['chains'], config['heldout_chains']
        if (not isinstance(chain_ids, list) or not chain_ids or not isinstance(probes, list)
                or any(not isinstance(c, str) or c not in core.chains() for c in chain_ids + probes)
                or len(set(chain_ids + probes)) != len(chain_ids + probes)):
            raise ValueError('chains_must_be_unique_disjoint_native_roots')
        if config['cohort_role'] not in ('development_seen', 'pre_registered_evaluation'):
            raise ValueError('cohort_role_required')
        if type(config['validation_repeats']) is not int or not 1 <= config['validation_repeats'] <= 3:
            raise ValueError('validation_repeats_out_of_bounds')
        ports = config['ports']
        if not ports or len(set(ports)) != len(ports) or any(type(p) is not int or p not in cross.PORTS for p in ports):
            raise ValueError('invalid_ports')
        from harbor.models.task.task import Task
        stream, heldout = [], []
        for chain in chain_ids + probes:
            dest, prefix = (stream, 's') if chain in chain_ids else (heldout, 'h')
            for version, task in enumerate(core.tasks(chain)):
                ok, reason = core.ready(task, Path(config['readiness_root']))
                if not ok:
                    raise RuntimeError('environment_not_preflighted:' + task + ':' + str(reason))
                ticket = readiness(Path(config['readiness_root']), task)
                path = core.DATASET / task
                dest.append({'uid': prefix + f'{len(dest):06d}', 'chain': chain, 'version': version,
                    'task': task, 'path': str(path), 'task_checksum': Task(path).checksum,
                    'environment_fingerprint': fingerprint(path), 'image': ticket['image'], 'image_id': ticket['image_id'],
                    'readiness_root': config['readiness_root']})
        m = {'schema': 'cross-chain-self-skill-v1', 'created': time.time(), 'request': config,
             'model': core.MODEL, 'solve': SOLVE, 'verifier_timeout': 600, 'memory': bank.LIMITS,
             'learning': learning.BUDGET, 'gate': {**gate.DEFAULT, 'repeats': config['validation_repeats']},
             'stream': stream, 'heldout': heldout, 'ports': ports,
             'protocol': 'one persistent skill bank, ordered stream, no chain reset; score before learning; only past native paired replays can select updates',
             'claims': 'development unless exposure independently audited; finite regression gate, not proof of generalization or equal-total-compute superiority'}
        bank.initialize(root / 'bank')
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': snapshot()})
        atomic_json(root / 'status.json', {'time': time.time(), 'state': 'prepared_not_running', 'completed': 0})
        return m


def verify(root):
    seal = read_json(root / 'seal.json')
    if not seal or seal['manifest_sha256'] != file_hash(root / 'manifest.json') or seal['code'] != snapshot():
        raise RuntimeError('frozen_protocol_or_code_changed')
    return read_json(root / 'manifest.json')


def save_record(path, value):
    old = read_json(path)
    signed = {**value, 'record_sha256': digest(value)}
    if old is not None and old != signed:
        raise RuntimeError('immutable_stage_record_changed')
    atomic_json(path, signed)


def read_record(path):
    value = read_json(path)
    if value is not None and value.get('record_sha256') != digest({k: v for k, v in value.items() if k != 'record_sha256'}):
        raise RuntimeError('stage_record_hash_mismatch')
    return value


def verify_files(hashes):
    for p, expected in hashes.items():
        if file_hash(Path(p)) != expected:
            raise RuntimeError('recorded_evidence_changed:' + p)


def completed_prefix(root, m, count):
    output = []
    for entry in m['stream'][:count]:
        r = read_record(root / 'stream' / entry['uid'] / 'record.json')
        if not r or r.get('status') != 'complete' or r.get('uid') != entry['uid']:
            raise RuntimeError('stream_predecessor_incomplete')
        if output and r['before'] != output[-1][1]['after']:
            raise RuntimeError('cross_chain_memory_lineage_broken')
        verify_files(r.get('artifact_hashes', {}))
        output.append((entry, r))
    return output


def learning_history(root, completed):
    values = []
    for entry, record in completed:
        folder = root / 'stream' / entry['uid']
        diagnosis = read_json(folder / 'update/diagnosis.json')
        if diagnosis:
            values.append({'source': read_json(folder / 'source.json'), 'diagnosis': diagnosis})
    return values


def step(root, m, index, solve=runtime.native_solve, proposer=learning.propose, checker=runtime.check_entry):
    entry = m['stream'][index]
    folder = root / 'stream' / entry['uid']
    prior = read_record(folder / 'record.json')
    if prior is not None:
        if prior.get('status') != 'complete' or prior.get('uid') != entry['uid']:
            raise RuntimeError('invalid_completed_stage_record')
        verify_files(prior.get('artifact_hashes', {}))
        return prior
    if (folder / 'blocked.json').exists():
        raise RuntimeError('stage_quarantined; inspect_saved_artifacts_before_explicit_recovery')
    completed = completed_prefix(root, m, index)
    expected = completed[-1][1]['after'] if completed else read_json(root / 'bank/versions/000000/metadata.json')
    before = read_json(folder / 'before.json')
    if before is not None and before != expected:
        raise RuntimeError('stage_parent_mismatch')
    before = expected
    current = read_json(root / 'bank/HEAD.json')
    intent = read_json(folder / 'commit_intent.json')
    allowed = [before] + ([intent['after']] if intent else [])
    if current not in allowed:
        raise RuntimeError('global_bank_not_at_registered_stream_position')
    checker(entry)
    atomic_json(folder / 'before.json', before)
    old, provenance = bank.load(root / 'bank', before)
    parent = folder / 'parent'
    bank.publish(parent, old, provenance, before['revision'], before['after_task'])
    scored = solve(entry, parent, before, folder / 'solve', m['ports'][0], 'stream')
    if scored.get('before') != before or scored.get('purpose') != 'stream' or scored.get('status') != 'complete':
        raise RuntimeError('invalid_stream_record')
    # Write once BEFORE updating; no repaired score overwrites this official score.
    saved_score = read_json(folder / 'scored.json')
    if saved_score is not None and saved_score != scored:
        raise RuntimeError('official_score_changed')
    atomic_json(folder / 'scored.json', scored)
    source = learning.source_from_record(entry, scored)
    previous_source = read_json(folder / 'source.json')
    if previous_source is not None and previous_source != source:
        raise RuntimeError('source_changed')
    atomic_json(folder / 'source.json', source)
    cases = gate.select_cases(entry, completed)
    prior_cases = read_json(folder / 'validation_cases.json')
    if prior_cases is not None and prior_cases != cases:
        raise RuntimeError('validation_cohort_changed')
    atomic_json(folder / 'validation_cases.json', cases)  # selected BEFORE proposal
    for case in cases:
        checker(case)
    update = proposer(folder / 'update', source, old, provenance, learning_history(root, completed),
                      m['model'], f"http://127.0.0.1:{m['ports'][0]}/v1")
    decision = {'accept': False, 'reason': 'no_evidence_reviewed_change', 'pairs': []}
    after = before
    if update['status'] == 'candidate':
        candidate = folder / 'candidate'
        candidate_head = bank.publish(candidate, update['payload'], update['provenance'], before['revision'] + 1, entry['uid'])
        decision = gate.evaluate(folder / 'validation', cases, parent, before, candidate, candidate_head,
                                 m['ports'], m['gate']['repeats'], solve)
        for pair in decision['pairs']:
            for arm in ('parent', 'candidate'):
                verify_files(pair[arm].get('hashes', {}))
        if decision['pairs']:
            checked = gate.decide(decision['pairs'], m['gate']['repeats'])
            if any(checked[k] != decision[k] for k in checked):
                raise RuntimeError('cached_validation_decision_changed')
        if decision['accept']:
            planned = {'before': before, 'after': candidate_head, 'candidate_sha256': digest(update['payload']),
                       'validation_sha256': file_hash(folder / 'validation/result.json')}
            if intent is not None and intent != planned:
                raise RuntimeError('commit_intent_changed')
            atomic_json(folder / 'commit_intent.json', planned)
            after = bank.promote(root / 'bank', before, update['payload'], update['provenance'], entry['uid'])
    if read_json(root / 'bank/HEAD.json') != after:
        raise RuntimeError('bank_changed_before_stage_finalization')
    artifacts = dict(scored.get('hashes', {}))
    for p in (folder / 'source.json', folder / 'scored.json', folder / 'before.json', folder / 'validation_cases.json'):
        artifacts[str(p)] = file_hash(p)
    for sub in ('update', 'validation', 'parent', 'candidate'):
        for p in (folder / sub).rglob('*.json'):
            if '/trials/' not in str(p):
                artifacts[str(p)] = file_hash(p)
    result = {**scored, 'chain': entry['chain'], 'version': entry['version'], 'after': after,
              'update_status': update['status'], 'promoted': decision['accept'], 'promotion_reason': decision['reason'],
              'learning_usage': update.get('accounting', {}), 'validation_pairs': len(decision['pairs']),
              'artifact_hashes': artifacts}
    save_record(folder / 'record.json', result)
    return read_record(folder / 'record.json')


def probe(root, m, solve=runtime.native_solve, checker=runtime.check_entry):
    completed = completed_prefix(root, m, len(m['stream']))
    if not m['heldout']:
        return []
    head = completed[-1][1]['after']
    if read_json(root / 'bank/HEAD.json') != head:
        raise RuntimeError('probe_requires_final_stream_version')
    payload, provenance = bank.load(root / 'bank', head)
    store = root / 'probe-bank'
    bank.publish(store, payload, provenance, head['revision'], head['after_task'])
    result = []
    for entry in m['heldout']:
        checker(entry)
        record = solve(entry, store, head, root / 'probes' / entry['uid'], m['ports'][0], 'probe')
        if record.get('status') != 'complete' or record['purpose'] != 'probe' or record['before'] != head:
            raise RuntimeError('invalid_probe_version')
        result.append(record)
        if read_json(root / 'bank/HEAD.json') != head or bank.load(store, head)[0] != payload:
            raise RuntimeError('probe_mutated_skill_library')
    atomic_json(root / 'probe_report.json', {'frozen': head, 'records': result,
        'mean_reward': sum(r['reward'] for r in result) / len(result), 'target_learning': False,
        'used_for_promotion': False})
    return result


def report(root, m):
    records = []
    for entry in m['stream']:
        value = read_record(root / 'stream' / entry['uid'] / 'record.json')
        if value:
            records.append(value)
    scored = [read_json(root / 'stream' / e['uid'] / 'scored.json') for e in m['stream']]
    scored = [r for r in scored if r]
    validation = [read_json(p) for p in (root / 'stream').glob('*/validation/*/*/*/record.json')]
    evolution = [r for r in scored if next(e for e in m['stream'] if e['uid'] == r['uid'])['version'] > 0]
    chains = {}
    for chain in m['request']['chains']:
        entries = [e for e in m['stream'] if e['chain'] == chain]
        ids = {e['uid'] for e in entries}
        values = [r['reward'] for r in scored if r['uid'] in ids]
        chains[chain] = {'scored': len(values), 'total': len(entries),
                         'complete': len(values) == len(entries),
                         'mean_reward': sum(values) / len(values) if values else None,
                         'all_passed': all(values) if len(values) == len(entries) else None}
    return {'time': time.time(), 'scored_tasks': len(scored), 'completed_update_steps': len(records),
        'total_tasks': len(m['stream']), 'promotions': sum(r['promoted'] for r in records),
        'official_mean_reward': sum(r['reward'] for r in scored) / len(scored) if scored else None,
        'evolved_version_mean_reward': sum(r['reward'] for r in evolution) / len(evolution) if evolution else None,
        'validation_solves_excluded_from_score': len(validation),
        'stream_seconds': sum(r.get('solve_seconds', 0) for r in scored),
        'validation_seconds': sum(r.get('solve_seconds', 0) for r in validation),
        'learning_calls': sum(len(list((root / 'stream' / e['uid'] / 'update').glob('*/request_started.json'))) for e in m['stream']),
        'learning_usage': {e['uid']: learning.accounting(root / 'stream' / e['uid'] / 'update') for e in m['stream']
                           if (root / 'stream' / e['uid'] / 'update').exists()},
        'bank': read_json(root / 'bank/HEAD.json'), 'chains': chains,
        'blocked': [read_json(p) for p in (root / 'stream').glob('*/blocked.json')], 'interpretation': m['claims']}


def heartbeat(root, stop):
    """Separate liveness file: a long solve must not look like a dead scheduler."""
    while not stop.is_set():
        atomic_json(root / 'heartbeat.json', {'time': time.time(), 'pid': os.getpid(),
            'identity': core.process_identity(os.getpid()), 'status': read_json(root / 'status.json')})
        stop.wait(20)


def run(root, *, probes_only=False):
    stop = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(root, stop), daemon=True)
    # Only the owner of the scheduler lock may publish heartbeat/launch state.
    with legacy.exclusive(root / 'scheduler.lock'):
        thread.start()
        try:
            run_locked(root, probes_only=probes_only)
        finally:
            stop.set()
            thread.join(timeout=5)


def run_locked(root, *, probes_only=False):
    m = verify(root)
    with runtime.inference_lease(m['ports']):
        if (root / 'PAUSE').exists():
            raise RuntimeError('campaign_paused')
        # Never start another trial behind an orphan left by a dead controller.
        for p in root.rglob('child.json'):
            child = read_json(p)
            if legacy.alive(child):
                raise RuntimeError('orphan_native_trial_still_alive:' + str(p))
        stopping = False
        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        if probes_only:
            probe(root, m)
            return
        for index, entry in enumerate(m['stream']):
            verify(root)
            if stopping or (root / 'PAUSE').exists():
                atomic_json(root / 'status.json', {'time': time.time(), 'state': 'paused', 'completed': index})
                return
            status = {'time': time.time(), 'pid': os.getpid(), 'identity': core.process_identity(os.getpid()),
                      'state': 'running', 'uid': entry['uid'], 'task': entry['task'], 'completed': index}
            atomic_json(root / 'status.json', status)
            try:
                step(root, m, index)
            except Exception as exc:
                blocked = {**status, 'state': 'blocked', 'reason': f'{type(exc).__name__}:{exc}',
                           'automatic_retry': False, 'score_is_not_overwritten': True}
                atomic_json(root / 'stream' / entry['uid'] / 'blocked.json', blocked)
                atomic_json(root / 'status.json', blocked)
                atomic_json(root / 'metrics.json', report(root, m))
                raise
            atomic_json(root / 'metrics.json', report(root, m))
        atomic_json(root / 'status.json', {'time': time.time(), 'state': 'complete', 'completed': len(m['stream'])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ('prepare', 'run', 'report', 'probe'):
        actions.add_argument('--' + action, action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--allow-native-runs', action='store_true', help='Explicitly spend model/verification compute; does not pause other campaigns.')
    args = parser.parse_args()
    if args.prepare:
        if not args.config:
            parser.error('--prepare requires --config')
        m = prepare(args.root, args.config)
        print(json.dumps({'prepared': True, 'tasks': len(m['stream']), 'heldout_tasks': len(m['heldout']),
                          'native_runs_started': False, 'persistent_bank': str(args.root / 'bank')}, ensure_ascii=False))
    elif args.report:
        print(json.dumps(report(args.root, verify(args.root)), ensure_ascii=False, indent=2))
    elif not args.allow_native_runs:
        parser.error('native calls require explicit --allow-native-runs; preparation/report never launch trials')
    else:
        run(args.root, probes_only=args.probe)


if __name__ == '__main__':
    main()
