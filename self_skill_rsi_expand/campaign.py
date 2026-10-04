"""Immutable 441-task, three-arm continuation; old results are never replayed."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import time

from rsi_harness.clean_rsi import read_json, load_payload
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_accel.campaign import clone_prefix
from self_skill_rsi_network.campaign import copy_frozen
from self_skill_rsi_storage import campaign as old
from self_skill_rsi_netguard import health
from .plan import SOURCE, CONTROL, ARMS

base, v2 = old.base, old.v2
PORTS, GROUPS = [8041, 8040], [[4, 5, 6, 7], [1, 2, 3, 8]]


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def control_record(root, e, arm):
    return root / 'controls' / e['chain'] / arm / 'stages' / e['task'] / 'record.json'


def prepare(root, deployment):
    if (root / 'manifest.json').exists(): return verify(root)
    v2.check_source_stopped(SOURCE)
    previous = old.verify(SOURCE)
    if read_json(SOURCE / 'status.json')['state'] != 'complete': raise RuntimeError('source_not_complete')
    completed = base.completed_prefix(SOURCE, previous, 112)
    queue = read_json(root / 'queue.json')
    if queue['source_manifest_sha256'] != file_hash(SOURCE / 'manifest.json'): raise RuntimeError('queue_source_changed')
    deployed = read_json(deployment)
    if (deployed['ports'] != PORTS or deployed['gpu_groups'] != GROUPS or deployed['model'] != previous['model']
            or not deployed['functional_checks_passed']): raise RuntimeError('wrong_inference_deployment')
    base.verify_files(deployed['evidence_hashes'])
    frozen = clone_prefix(SOURCE, root, completed)
    for name in ('matched-controls.json', 'network-incident.json', 'boundary-admission.json'):
        copy_frozen(SOURCE / name, root / name, frozen)
    all_entries = queue['prior'] + queue['remaining']
    matched = {e['task'] for e in queue['prior']}
    reused, excluded = {a: [] for a in ARMS[:2]}, []
    for chain in dict.fromkeys(e['chain'] for e in all_entries):
        for arm in ARMS[:2]:
            work = CONTROL / chain / arm
            last = None
            for task in base.core.tasks(chain):
                src = work / 'stages' / task / 'record.json'
                record = read_json(src, {})
                if record.get('status') != 'complete': break
                if base.core.baseline_compatibility(Path(record['result']), task):
                    raise RuntimeError('existing_control_incompatible:' + arm + ':' + task)
                if task not in matched:
                    trace = Path(record['result']).parent / 'agent/trajectory.json'
                    issues = health.trace_evidence(trace)['transport_failures']
                    if issues:
                        excluded.append({'arm': arm, 'task': task, 'reason': 'observed_transport_failure', 'source': str(src)})
                        break
                e = next(x for x in all_entries if x['task'] == task)
                dst = control_record(root, e, arm)
                copy_frozen(src, dst, frozen)
                reused[arm].append(task)
                last = record
            if arm == 'skill-rsi':
                head = ((last.get('improvement') or {}).get('after') or last.get('before')) if last else None
                if head:
                    source_state = work / 'state'
                    load_payload(source_state, head)
                    target_state = root / 'controls' / chain / arm / 'state'
                    for p in (source_state / 'versions').iterdir():
                        if p.is_dir() and int(p.name) <= head['revision']:
                            copy_frozen(p, target_state / 'versions' / p.name, frozen)
                    atomic_json(target_state / 'HEAD.json', head)
                elif last:
                    raise RuntimeError('missing_control_memory_lineage:' + chain)
    # Look for compatible older baseline solves as well as the clean campaign.
    # They are stateless, but still need the same solver configuration and a clean trace.
    for e in queue['remaining']:
        if e['task'] in reused['baseline']: continue
        record = base.core.legacy_baseline(base.core.OLD_ROOT, e['chain'], e['task'])
        if not record or record.get('status') != 'complete': continue
        path = Path(record['result'])
        if health.trace_evidence(path.parent / 'agent/trajectory.json')['transport_failures']: continue
        atomic_json(control_record(root, e, 'baseline'), {**record, 'task': e['task']})
        for p in (control_record(root, e, 'baseline'), path, path.parent / 'agent/trajectory.json'):
            frozen[str(p)] = file_hash(p)
        reused['baseline'].append(e['task'])
    for p in (SOURCE / 'manifest.json', SOURCE / 'seal.json', root / 'queue.json', deployment):
        frozen[str(p)] = file_hash(p)
    frozen.update(deployed['evidence_hashes'])
    m = copy.deepcopy(previous)
    m['ports'] = PORTS
    m['stream'] = all_entries
    m['request']['chains'] = list(dict.fromkeys(e['chain'] for e in all_entries))
    m['inference_migration']['deployment'] = str(deployment)
    m['extension'] = {'source_root': str(SOURCE), 'created': time.time(), 'implementation': code(),
        'frozen_files': frozen, 'preserved_self_skill_tasks': 112, 'remaining_tasks': 329,
        'arms': ARMS, 'reused_controls': reused, 'excluded_controls': excluded,
        'selection': queue['selection'], 'generation_configuration_changed': False,
        'self_skill_bank_reset': False, 'ordinary_skill_chain_reset': True,
        'scheduler': 'two TP4 services; one active step per arm; lagging-prefix priority; chain-order preserved',
        'interpretation': queue['interpretation'],
        'environment_policy': 'whole-chain immutable offline acceptance before admission; failed infrastructure is not a zero'}
    atomic_json(root / 'manifest.json', m)
    atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': v2.snapshot()})
    atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time(), 'completed': 112})
    return verify(root)


def verify(root):
    m, seal = read_json(root / 'manifest.json'), read_json(root / 'seal.json')
    if (seal['manifest_sha256'] != file_hash(root / 'manifest.json') or seal['code'] != v2.snapshot()
            or m['extension']['implementation'] != code()): raise RuntimeError('extension_seal_changed')
    v2.check_source_stopped(SOURCE)
    previous = old.verify(SOURCE)
    q = read_json(root / 'queue.json')
    if m['stream'] != previous['stream'] + q['remaining'] or any(m[k] != previous[k] for k in ('solve', 'model', 'memory', 'learning', 'gate')):
        raise RuntimeError('unregistered_algorithm_change')
    base.verify_files(m['extension']['frozen_files'])
    return m


def acceptance(root, chain):
    receipt = read_json(root / 'environment-ready' / f'{chain}.json')
    if not receipt: return None
    path = Path(receipt['acceptance'])
    if file_hash(path) != receipt['sha256']: raise RuntimeError('environment_acceptance_changed')
    value = read_json(path)
    if not value['passed'] or file_hash(Path(value['grader'])) != value['grader_sha256']:
        raise RuntimeError('invalid_environment_acceptance')
    if chain == 'configure-git-webserver':
        bridge = read_json(path.parent / 'bridge-check.json', {})
        if not bridge.get('passed') or bridge.get('grader_sha256') != value['grader_sha256']:
            return None
    return value


def resolved_manifest(root, m):
    result = copy.deepcopy(m)
    registry = {}
    for chain in read_json(root / 'queue.json')['chain_order']:
        value = acceptance(root, chain)
        if value:
            registry.update({e['task']: e for e in value['entries']})
    for i in range(112, len(result['stream'])):
        entry = result['stream'][i]
        if entry['task'] in registry:
            resolved = registry[entry['task']]
            if any(resolved.get(k) != entry[k] for k in ('uid', 'task', 'chain', 'version', 'path', 'task_checksum', 'environment_fingerprint')):
                raise RuntimeError('environment_registered_wrong_task')
            result['stream'][i] = resolved
    return result


def official(root, e, arm, m):
    if arm == 'self-skill': return read_json(root / 'stream' / e['uid'] / 'scored.json')
    record = read_json(control_record(root, e, arm))
    if not record or record.get('status') != 'complete': return None
    if e['task'] not in m['extension']['reused_controls'][arm]:
        admitted = read_json(control_record(root, e, arm).parent / 'network-admission.json')
        if not admitted or not admitted['accepted']: return None
    return record


def finished(root, e, arm, m):
    if arm == 'self-skill': return (root / 'stream' / e['uid'] / 'record.json').exists()
    return official(root, e, arm, m) is not None


def next_entry(root, m, arm):
    return next((e for e in m['stream'][112:] if not finished(root, e, arm, m)), None)


def report(root, m):
    self_report = v2.report(root, m)
    rows = {a: {e['task']: official(root, e, a, m) for e in m['stream']} for a in ARMS}
    common = [e for e in m['stream'] if all(rows[a][e['task']] is not None for a in ARMS)]
    summary = {}
    for arm in ARMS:
        done = [x for x in rows[arm].values() if x is not None]
        summary[arm] = {'scored': len(done), 'passed': sum(x['reward'] for x in done),
            'completed_steps': sum(finished(root, e, arm, m) for e in m['stream']),
            'next_task': (next_entry(root, m, arm) or {}).get('task')}
    paired = {'n': len(common), 'baseline_passed': sum(rows['baseline'][e['task']]['reward'] for e in common),
        'ordinary_skill_passed': sum(rows['skill-rsi'][e['task']]['reward'] for e in common),
        'self_skill_passed': sum(rows['self-skill'][e['task']]['reward'] for e in common),
        'interpretation': m['extension']['interpretation']}
    extension = [e for e in common if e['uid'] >= 's000112']
    return {**self_report, 'arms': summary, 'historical_matched_comparison': paired,
        'extension_paired': {'n': len(extension), **{a: sum(rows[a][e['task']]['reward'] for e in extension) for a in ARMS}},
        'inference': {'ports': PORTS, 'gpu_groups': GROUPS, 'concurrent_arm_steps': 2,
                      'candidate_validation_parallelism_per_arm': 1, 'official_stream_parallelism_per_arm': 1},
        'environment_progress': read_json(root / 'environment-progress.json', {}),
        'environment_ready_chains': len(list((root / 'environment-ready').glob('*.json'))),
        'environment_errors': [p.stem for p in sorted((root / 'environment-errors').glob('*.json'))
                               if not (root / 'environment-ready' / p.name).exists()]}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path)
    p.add_argument('--deployment', type=Path); p.add_argument('--prepare', action='store_true'); p.add_argument('--report', action='store_true')
    a = p.parse_args(); r = a.root.resolve()
    if a.prepare: prepare(r, a.deployment.resolve())
    elif a.report: print(json.dumps(report(r, verify(r)), indent=2))
    else: p.error('explicit action required')
