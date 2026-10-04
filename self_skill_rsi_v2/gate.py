"""Reuse original official scores; execute ONLY the candidate library.

Ties, including 0 -> 0, may be admitted after evidence review. This is a finite
non-regression screen, not proof of effectiveness or a causal paired estimate.
The current reference used the immediate parent library. Historical references
may use older libraries and are explicitly retention checks, not fresh controls.
"""
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path

from rsi_harness.clean_rsi import digest, read_json, select_skills
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi import bank, campaign as base
from self_skill_rsi.gate import select_cases

DEFAULT = {'policy': 'saved-official-score-nonregression-v2', 'max_cases': 2,
           'repeats': 1, 'parent_replays': 0, 'accept_ties': True,
           'interpretation': 'admitted on observed non-regression; benefit and transfer remain unproven'}


def validate_record(entry, record, purpose, head=None):
    if (record.get('status') != 'complete' or record.get('purpose') != purpose
            or record.get('uid') != entry['uid'] or record.get('task') != entry['task']
            or type(record.get('reward')) not in (int, float)
            or not math.isfinite(record['reward']) or record['reward'] not in (0, 1)
            or (head is not None and record.get('before') != head)):
        raise RuntimeError('invalid_or_mismatched_score_is_not_algorithmic_failure')
    if not record.get('hashes') or record.get('result') not in record['hashes']:
        raise RuntimeError('missing_native_score_provenance')
    base.verify_files(record['hashes'])


def decide(comparisons):
    if not comparisons:
        raise ValueError('empty_validation')
    deltas, seen = {}, set()
    for row in comparisons:
        uid = row['uid']
        if uid in seen:
            raise ValueError('duplicate_validation_case')
        seen.add(uid)
        entry = {'uid': uid, 'task': row['task']}
        validate_record(entry, row['reference'], 'stream')
        validate_record(entry, row['candidate'], 'validation')
        deltas[uid] = row['candidate']['reward'] - row['reference']['reward']
    accepted = all(d >= 0 for d in deltas.values())
    gain = any(d > 0 for d in deltas.values())
    return {'accept': accepted,
            'reason': ('non_regression_with_gain' if gain else 'non_regression_tie') if accepted else 'regression',
            'case_deltas': deltas, 'observed_score_gain': gain,
            'effectiveness_proven': False, 'interpretation': DEFAULT['interpretation']}


def evaluate(folder, cases, references, old_store, old_head, new_store, new_head, ports, solve):
    if not cases or len({e['uid'] for e in cases}) != len(cases):
        raise ValueError('invalid_validation_cases')
    if not ports or len(set(ports)) != len(ports):
        raise ValueError('invalid_validation_ports')
    if set(references) != {e['uid'] for e in cases}:
        raise ValueError('reference_cohort_mismatch')
    for entry in cases:
        validate_record(entry, references[entry['uid']], 'stream')
    if references[cases[0]['uid']]['before'] != old_head:
        raise RuntimeError('current_official_score_did_not_use_parent_library')
    old, _ = bank.load(old_store, old_head)
    new, _ = bank.load(new_store, new_head)
    spec = {'policy': DEFAULT, 'cases': cases, 'parent': old_head, 'candidate': new_head,
            'reference_hashes': {uid: digest(r) for uid, r in references.items()}}
    prior = read_json(folder / 'plan.json')
    if prior is not None and prior != spec:
        raise RuntimeError('validation_plan_changed')
    atomic_json(folder / 'plan.json', spec)
    retrieval = {e['uid']: {'parent': select_skills(old, Path(e['path']).joinpath('instruction.md').read_text()),
                            'candidate': select_skills(new, Path(e['path']).joinpath('instruction.md').read_text())}
                 for e in cases}
    if all(v['parent'][1] == v['candidate'][1] for v in retrieval.values()):
        result = {'accept': False, 'reason': 'candidate_not_retrieved_on_validation_cases',
                  'comparisons': [], 'retrieval': retrieval, 'effectiveness_proven': False}
        atomic_json(folder / 'result.json', result)
        return result
    previous = read_json(folder / 'result.json')
    if previous is not None:
        comparisons = previous['comparisons']
        if [r['uid'] for r in comparisons] != [e['uid'] for e in cases]:
            raise RuntimeError('cached_validation_cohort_changed')
        for row in comparisons:
            if row['reference'] != references[row['uid']]:
                raise RuntimeError('cached_reference_changed')
            validate_record(row, row['candidate'], 'validation', new_head)
        decision = decide(comparisons)
        if any(previous.get(k) != v for k, v in decision.items()):
            raise RuntimeError('cached_validation_decision_changed')
        return previous

    def lane(index, port):
        values = {}
        for entry in cases[index::len(ports)]:
            job = folder / entry['uid'] / '00/candidate'
            record = read_json(job / 'record.json')
            if record is None:
                record = solve(entry, new_store, new_head, job, port, 'validation')
            validate_record(entry, record, 'validation', new_head)
            values[entry['uid']] = record
        return values

    outcomes = {}
    with ThreadPoolExecutor(max_workers=min(len(ports), len(cases))) as executor:
        futures = [executor.submit(lane, i, p) for i, p in enumerate(ports[:len(cases)])]
        for future in futures:
            outcomes.update(future.result())
    comparisons = [{'uid': e['uid'], 'task': e['task'], 'reference': references[e['uid']],
                    'candidate': outcomes[e['uid']], 'reference_kind': 'saved_official_score',
                    'same_parent_version': references[e['uid']]['before'] == old_head} for e in cases]
    result = {**decide(comparisons), 'comparisons': comparisons, 'retrieval': retrieval,
              'parent_replays': 0, 'candidate_replays': len(comparisons)}
    atomic_json(folder / 'result.json', result)
    return result
