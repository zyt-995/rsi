"""Append control-matched tasks, preserving the complete old learning history.

This is a scheduling/cohort extension, NOT a new learning algorithm. The exact
v2 step, original agent, budgets, retrieval, and candidate-only gate are reused.
Future tasks without an accepted offline grader stop before any model call.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import signal
import threading
import time
from functools import partial

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash, fingerprint
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_resilient import campaign as old, runtime, grading

base, v2 = old.base, old.v2
POLICY = old.POLICY
SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-rsi-20261001-resilient-v1')
CONTROL = base.core.PROJECT / 'runs/clean-rsi-20260923-qwen38-27b'
SCHEMA = 'self-skill-control-matched-continuation-v1'


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def matched_tasks(control, seen):
    """Select by completion/configuration, never by reward or model trace."""
    schedule = read_json(control / 'scheduler_manifest.json')
    rows, excluded = [], []
    for chain in schedule['chain_order']:
        prefix_complete = True
        for version, task in enumerate(schedule['task_order'][chain]):
            records = {a: control / chain / a / 'stages' / task / 'record.json'
                       for a in ('baseline', 'skill-rsi')}
            values = {a: read_json(p, {}) for a, p in records.items()}
            if not all(r.get('status') == 'complete' for r in values.values()):
                prefix_complete = False
                continue
            issues = {a: base.core.baseline_compatibility(Path(r['result']), task)
                      for a, r in values.items()}
            if any(issues.values()):
                prefix_complete = False
                excluded.append({'task': task, 'issues': issues})
                continue
            rows.append({'task': task, 'chain': chain, 'version': version,
                         'already_scored_self_skill': task in seen,
                         'chain_prefix_complete': prefix_complete,
                         'controls': {a: {'record': str(records[a].resolve()),
                            'record_sha256': file_hash(records[a]), 'result': r['result'],
                            'result_sha256': file_hash(Path(r['result'])), 'reward': r['reward']}
                            for a, r in values.items()}})
    return {'selection': 'existing_control_order_and_completed_compatible_intersection_not_rewards',
            'common': rows, 'excluded': excluded,
            'additional': [r for r in rows if not r['already_scored_self_skill'] and r['chain_prefix_complete']]}


def entries(rows, offset):
    from harbor.models.task.task import Task
    result = []
    for i, row in enumerate(rows):
        path = base.core.DATASET / row['task']
        ticket = base.core.readiness(base.core.OLD_ROOT, row['task'])
        if ticket.get('status') != 'ready':
            raise RuntimeError('missing_prior_environment_ticket:' + row['task'])
        result.append({'uid': f's{offset+i:06d}', 'chain': row['chain'],
            'version': row['version'], 'task': row['task'], 'path': str(path),
            'task_checksum': Task(path).checksum, 'environment_fingerprint': fingerprint(path),
            'image': ticket['image'], 'image_id': ticket['image_id'],
            'readiness_root': str(base.core.OLD_ROOT)})
    return result


def prepare(root, deployment, grader, acceptance):
    root = root.resolve()
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('new_continuation_directory_required')
        source = old.verify(SOURCE)
        if read_json(SOURCE / 'status.json').get('state') != 'complete':
            raise RuntimeError('source_not_complete')
        for name in ('supervisor_launch.json', 'watchdog_launch.json'):
            if base.legacy.alive(read_json(SOURCE / name, {})):
                raise RuntimeError('source_process_still_live')
        completed = base.completed_prefix(SOURCE, source, len(source['stream']))
        if read_json(SOURCE / 'bank/HEAD.json') != completed[-1][1]['after']:
            raise RuntimeError('source_skill_lineage_mismatch')
        selection = matched_tasks(CONTROL, {e['task'] for e in source['stream']})
        appended = entries(selection['additional'], len(source['stream']))
        if not appended:
            raise RuntimeError('no_additional_matched_tasks')
        checks, deployed = read_json(acceptance), read_json(deployment)
        if not checks.get('passed') or checks['grader_sha256'] != file_hash(grader):
            raise RuntimeError('offline_grader_acceptance_missing')
        safe = grading.load_manifest(grader, file_hash(grader))
        if appended[0]['task'] not in safe['tasks']:
            raise RuntimeError('first_task_has_no_grader')
        if (deployed['ports'] != [8041] or deployed['gpu_groups'] != [[4,5,6,7]]
                or deployed['model'] != source['model'] or not deployed['functional_checks_passed']):
            raise RuntimeError('unregistered_inference_deployment')
        frozen = old.prior.clone_prefix(SOURCE, root, completed)
        for p in (SOURCE/'manifest.json', SOURCE/'seal.json', deployment, grader, acceptance,
                  CONTROL/'manifest.json', CONTROL/'scheduler_manifest.json'):
            frozen[str(p.resolve())] = file_hash(p)
        frozen.update(deployed['evidence_hashes'])
        for row in selection['common']:
            for r in row['controls'].values():
                frozen[r['record']] = r['record_sha256']
                frozen[r['result']] = r['result_sha256']
        atomic_json(root / 'matched-controls.json', selection)
        frozen[str(root / 'matched-controls.json')] = file_hash(root / 'matched-controls.json')
        m = copy.deepcopy(source)
        m['schema'] = SCHEMA
        m['ports'] = [8041]
        m['stream'] += appended
        m['request']['chains'] += list(dict.fromkeys(e['chain'] for e in appended))
        m['inference_migration']['deployment'] = str(deployment.resolve())
        m['resilience_migration']['grader'] = str(grader.resolve())
        m['resilience_migration']['grader_sha256'] = file_hash(grader)
        m['continuation'] = {'source_root': str(SOURCE), 'created': time.time(),
            'preserved_count': len(completed), 'added_count': len(appended),
            'ready_grader_tasks': [e['task'] for e in appended if e['task'] in safe['tasks']],
            'algorithm_changed': False, 'formal_repeats': 0,
            'budget_prompt_added': False, 'candidate_validation_parallelism': 1,
            'interpretation': 'historical paired-task comparison; original controls reset skill at chain boundaries; '
                'self-skill keeps cross-chain history and has additional update/validation compute; '
                'inference topology/grading infrastructure differ; not an isolated algorithmic causal comparison',
            'frozen_files': frozen, 'implementation': code(), 'policy': POLICY}
        atomic_json(root/'manifest.json', m)
        atomic_json(root/'seal.json', {'manifest_sha256': file_hash(root/'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root/'status.json', {'state': 'prepared_not_running', 'time': time.time(),
                    'completed': len(completed)})
        return verify(root)


def verify(root):
    m, seal = read_json(root/'manifest.json'), read_json(root/'seal.json')
    if (seal['manifest_sha256'] != file_hash(root/'manifest.json') or seal['code'] != v2.snapshot()
            or m['schema'] != SCHEMA or m['continuation']['implementation'] != code()
            or m['continuation']['policy'] != POLICY):
        raise RuntimeError('continuation_seal_changed')
    source = old.verify(Path(m['continuation']['source_root']))
    n = m['continuation']['preserved_count']
    if m['stream'][:n] != source['stream'] or any(m[k] != source[k] for k in
            ('model', 'solve', 'memory', 'learning', 'gate', 'verifier_timeout')):
        raise RuntimeError('old_algorithm_or_history_changed')
    base.verify_files(m['continuation']['frozen_files'])
    grading.load_manifest(m['resilience_migration']['grader'], m['resilience_migration']['grader_sha256'])
    return m


def report(root, m):
    result = v2.report(root, m)
    selection = read_json(root/'matched-controls.json')
    by_task = {e['task']: read_json(root/'stream'/e['uid']/'scored.json') for e in m['stream']}
    paired = [r for r in selection['common'] if by_task.get(r['task'])]
    result['historical_matched_comparison'] = {'n': len(paired),
        'baseline_passed': sum(r['controls']['baseline']['reward'] for r in paired),
        'ordinary_skill_passed': sum(r['controls']['skill-rsi']['reward'] for r in paired),
        'self_skill_passed': sum(by_task[r['task']]['reward'] for r in paired),
        'interpretation': m['continuation']['interpretation']}
    n = m['continuation']['preserved_count']
    new = [by_task[e['task']] for e in m['stream'][n:] if by_task[e['task']]]
    result['continuation'] = {'preserved_scored_tasks': n, 'added_scored_tasks': len(new),
        'additional_target_tasks': m['continuation']['added_count'],
        'additional_passed': sum(r['reward'] for r in new),
        'ready_grader_tasks': m['continuation']['ready_grader_tasks'], 'algorithm_changed': False}
    return result


def run(root):
    with base.legacy.exclusive(root/'scheduler.lock'):
        m = verify(root)
        os.environ.update(runner_environment(m['ports'][0]))
        for p in root.glob('stream/**/child.json'):
            runtime.wait_child(p.parent)
        with old.prior.offline.runtime.inference_lease(m['ports']), old.adapter(m):
            stopping = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopping.set())
            signal.signal(signal.SIGINT, lambda *_: stopping.set())
            heart = threading.Thread(target=base.heartbeat, args=(root, stopping), daemon=True)
            heart.start()
            safe = grading.load_manifest(m['resilience_migration']['grader'], m['resilience_migration']['grader_sha256'])
            native = partial(runtime.native_solve, manifest=safe)
            try:
                for index, entry in enumerate(m['stream']):
                    if (root/'stream'/entry['uid']/'record.json').exists():
                        continue
                    if stopping.is_set() or (root/'PAUSE').exists():
                        atomic_json(root/'status.json', {'state':'paused', 'time':time.time(), 'completed':index})
                        return
                    verify(root)
                    if entry['task'] not in safe['tasks']:
                        atomic_json(root/'status.json', {'state':'awaiting_environment', 'time':time.time(),
                            'completed':index, 'task':entry['task'], 'reason':'next_chain_offline_grader_not_yet_accepted',
                            'new_model_calls_for_pending_task':0, 'automatic_model_replay':False})
                        return
                    status = {'state':'running', 'time':time.time(), 'stage_started':time.time(),
                        'pid':os.getpid(), 'identity':base.core.process_identity(os.getpid()),
                        'uid':entry['uid'], 'task':entry['task'], 'completed':index}
                    atomic_json(root/'status.json', status)
                    try:
                        v2.step(root, m, index, solve=native, proposer=runtime.propose)
                    except Exception as exc:
                        atomic_json(root/'status.json', {**status,'state':'needs_attention','time':time.time(),
                            'reason':type(exc).__name__+':'+str(exc),'invalid_is_not_zero':True,'automatic_model_replay':False})
                        return
                    finally:
                        atomic_json(root/'metrics.json', report(root,m))
                atomic_json(root/'status.json', {'state':'complete','time':time.time(),'completed':len(m['stream'])})
            finally:
                stopping.set()
                heart.join(timeout=5)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--prepare', action='store_true')
    p.add_argument('--deployment', type=Path)
    p.add_argument('--grader', type=Path)
    p.add_argument('--acceptance', type=Path)
    p.add_argument('--run', action='store_true')
    p.add_argument('--report', action='store_true')
    p.add_argument('--allow-native-runs', action='store_true')
    a = p.parse_args()
    if a.prepare:
        prepare(a.root, a.deployment, a.grader, a.acceptance)
    elif a.report:
        print(json.dumps(report(a.root, verify(a.root)), indent=2))
    elif a.run and a.allow_native_runs:
        run(a.root)
    else:
        p.error('explicit action required')
