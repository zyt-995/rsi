"""Paired native replays on PAST tasks only, never future-task selection."""
from concurrent.futures import ThreadPoolExecutor
import math

from rsi_harness.clean_rsi import read_json, select_skills
from rsi_harness.terminal_infra import atomic_json
from . import bank

DEFAULT = {'max_cases': 2, 'repeats': 1, 'allow_efficiency_promotion': False,
           'interpretation': 'finite pilot regression evidence, NOT a monotonic-improvement guarantee'}


def select_cases(current, completed):
    """Fixed before proposing. Prefer a previously passed OTHER chain as guard."""
    eligible = [(e, r) for e, r in completed if r['status'] == 'complete' and r.get('purpose') == 'stream']
    cross = [e for e, r in eligible if e['chain'] != current['chain'] and r['reward'] == 1]
    passed = [e for e, r in eligible if r['reward'] == 1]
    old = cross or passed or [e for e, _ in eligible]
    return [current] + ([old[-1]] if old else [])


def decide(pairs, repeats):
    if not pairs or type(repeats) is not int or repeats < 1:
        raise ValueError('empty_or_invalid_validation')
    sums = {}
    for row in pairs:
        key = (row['uid'], row['repeat'])
        if key in sums:
            raise ValueError('duplicate_validation_pair')
        a, b = row['parent'], row['candidate']
        for value in (a, b):
            if (value.get('status') != 'complete' or value.get('purpose') != 'validation'
                    or value.get('uid') != row['uid']
                    or type(value.get('reward')) not in (float, int) or not math.isfinite(value['reward'])
                    or value['reward'] not in (0, 1)):
                raise RuntimeError('invalid_validation_is_not_algorithmic_failure')
        sums[key] = (a['reward'], b['reward'])
    cases = sorted({uid for uid, _ in sums})
    if set(sums) != {(uid, repeat) for uid in cases for repeat in range(repeats)}:
        raise ValueError('incomplete_validation_repeats')
    deltas = {uid: sum(sums[uid, r][1] - sums[uid, r][0] for r in range(repeats)) for uid in cases}
    accept = all(delta >= 0 for delta in deltas.values()) and any(delta > 0 for delta in deltas.values())
    return {'accept': accept, 'reason': 'paired_reward_gain_without_case_regression' if accept else
            ('regression' if any(d < 0 for d in deltas.values()) else 'no_measured_reward_gain'),
            'case_deltas': deltas, 'repeats': repeats, 'interpretation': DEFAULT['interpretation']}


def evaluate(folder, cases, old_store, old_head, new_store, new_head, ports, repeats, solve):
    if not cases or len({e['uid'] for e in cases}) != len(cases):
        raise ValueError('invalid_validation_cases')
    if not ports or len(set(ports)) != len(ports):
        raise ValueError('invalid_validation_ports')
    old, _ = bank.load(old_store, old_head)
    new, _ = bank.load(new_store, new_head)
    spec = {'cases': cases, 'parent': old_head, 'candidate': new_head, 'repeats': repeats}
    prior = read_json(folder / 'plan.json')
    if prior is not None and prior != spec:
        raise RuntimeError('validation_plan_changed')
    atomic_json(folder / 'plan.json', spec)
    from pathlib import Path
    retrieval = {e['uid']: {'parent': select_skills(old, Path(e['path']).joinpath('instruction.md').read_text()),
                            'candidate': select_skills(new, Path(e['path']).joinpath('instruction.md').read_text())}
                 for e in cases}
    if all(v['parent'][1] == v['candidate'][1] for v in retrieval.values()):
        result = {'accept': False, 'reason': 'candidate_not_retrieved_on_validation_cases', 'pairs': [], 'retrieval': retrieval}
        atomic_json(folder / 'result.json', result)
        return result
    old_result = read_json(folder / 'result.json')
    if old_result is not None:
        expected = {(e['uid'], r) for e in cases for r in range(repeats)}
        if {(p['uid'], p['repeat']) for p in old_result['pairs']} != expected:
            raise RuntimeError('cached_validation_cohort_changed')
        for pair in old_result['pairs']:
            if pair['parent']['before'] != old_head or pair['candidate']['before'] != new_head:
                raise RuntimeError('cached_validation_version_changed')
        return old_result
    jobs = [(e, repeat, arm) for e in cases for repeat in range(repeats) for arm in ('parent', 'candidate')]
    outcomes = {}
    # Each lane owns one endpoint. Commits remain strictly sequential regardless
    # of validation-completion order. All submitted jobs finish before promotion.
    def lane(index, port):
        values = {}
        for entry, repeat, arm in jobs[index::len(ports)]:
            store, head = (old_store, old_head) if arm == 'parent' else (new_store, new_head)
            job = folder / entry['uid'] / f'{repeat:02d}' / arm
            result = solve(entry, store, head, job, port, 'validation')
            if result.get('before') != head:
                raise RuntimeError('validation_consumed_wrong_skill_version')
            values[entry['uid'], repeat, arm] = result
        return values
    with ThreadPoolExecutor(max_workers=len(ports)) as executor:
        futures = [executor.submit(lane, i, p) for i, p in enumerate(ports)]
        for future in futures:
            outcomes.update(future.result())
    pairs = [{'uid': e['uid'], 'task': e['task'], 'repeat': repeat,
              'parent': outcomes[e['uid'], repeat, 'parent'], 'candidate': outcomes[e['uid'], repeat, 'candidate']}
             for e in cases for repeat in range(repeats)]
    result = {**decide(pairs, repeats), 'pairs': pairs, 'retrieval': retrieval}
    atomic_json(folder / 'result.json', result)
    return result
