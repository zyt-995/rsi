"""Boundary migration to two TP4 inference services, without repeating work."""
import argparse
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import time

from offline_verifier.profiles import sha
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi_offline import campaign as offline

base, v2 = offline.base, offline.v2


def code():
    return {str(p.resolve()): sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def boundary(source, m):
    """Accept only a complete, contiguous prefix; refuse any pending work."""
    count = 0
    pending = False
    for e in m['stream']:
        stage = source / 'stream' / e['uid']
        if not stage.exists():
            pending = True
            continue
        if pending or not (stage / 'record.json').exists():
            raise RuntimeError('not_a_completed_task_boundary')
        count += 1
    completed = base.completed_prefix(source, m, count)
    if not completed or count == len(m['stream']):
        raise RuntimeError('no_completed_prefix_or_no_remaining_tasks')
    if read_json(source / 'bank/HEAD.json') != completed[-1][1]['after']:
        raise RuntimeError('skill_bank_lineage_mismatch')
    return completed


def clone_prefix(source, root, completed):
    """Small artifact copies, with absolute original evidence paths unchanged."""
    frozen = {}
    for relative in [Path('bank')] + [Path('stream') / e['uid'] for e, _ in completed]:
        src, dest = source / relative, root / relative
        shutil.copytree(src, dest)
        for p in src.rglob('*'):
            if p.is_file():
                value = sha(p)
                cloned = dest / p.relative_to(src)
                if sha(cloned) != value:
                    raise RuntimeError('artifact_copy_mismatch')
                frozen[str(p)] = frozen[str(cloned)] = value
    # HEAD is the sole copied mutable pointer. Historical versions stay frozen.
    frozen.pop(str(root / 'bank/HEAD.json'))
    return frozen


def prepare(root, source, deployment):
    root, source, deployment = root.resolve(), source.resolve(), deployment.resolve()
    if root == source or root in source.parents or source in root.parents:
        raise ValueError('separate_run_directories_required')
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            m = verify(root)
            if m['inference_migration']['source_root'] != str(source):
                raise RuntimeError('migration_source_changed')
            return m
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('migration_target_not_empty')
        v2.check_source_stopped(source)
        old = offline.verify(source)
        completed = boundary(source, old)
        deployed = read_json(deployment)
        if (deployed.get('ports') != [8040, 8041] or deployed.get('gpu_groups') != [[0,1,2,3], [4,5,6,7]]
                or deployed.get('model') != old['model'] or not deployed.get('functional_checks_passed')
                or deployed.get('median_decode_speedup', 0) <= 1.05):
            raise RuntimeError('deployment_acceptance_missing')
        base.verify_files(deployed['evidence_hashes'])
        frozen = clone_prefix(source, root, completed)
        for p in (source / 'manifest.json', source / 'seal.json', deployment):
            frozen[str(p)] = sha(p)
        frozen.update(deployed['evidence_hashes'])
        base.completed_prefix(root, old, len(completed))
        m = copy.deepcopy(old)
        m['ports'] = [8040, 8041]
        m['inference_migration'] = {
            'source_root': str(source), 'created': time.time(), 'deployment': str(deployment),
            'preserved_official_uids': [e['uid'] for e, _ in completed],
            'first_accelerated_solve_uid': old['stream'][len(completed)]['uid'],
            'frozen_source_files': frozen, 'implementation': code(),
            'algorithm_changed': False, 'task_order_changed': False, 'generation_budgets_changed': False,
            'interpretation': 'inference deployment transition, TP2 eager to TP4 decode CUDA Graph; '
                              'same BF16 checkpoint and experiment protocol; numerical identity is not guaranteed'}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': sha(root / 'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time(),
            'completed': len(completed), 'scored_tasks': len(completed), 'new_model_calls': 0})
        return verify(root)


def verify(root):
    m = offline.verify(root)
    change = m['inference_migration']
    if change['implementation'] != code():
        raise RuntimeError('inference_adapter_code_changed')
    base.verify_files(change['frozen_source_files'])
    v2.check_source_stopped(Path(change['source_root']))
    original = read_json(Path(change['source_root']) / 'manifest.json')
    without_change = {k:v for k,v in m.items() if k != 'inference_migration'}
    without_change['ports'] = original['ports']
    if without_change != original or m['ports'] != [8040,8041]:
        raise RuntimeError('migration_changed_more_than_inference_deployment')
    return m


def report(root, m):
    result = v2.report(root, m)
    previous = set(m['inference_migration']['preserved_official_uids'])
    groups = {}
    for label, prior in [('preserved_tp2_scores', True), ('new_tp4_scores', False)]:
        values = [read_json(root / 'stream' / e['uid'] / 'scored.json')
                  for e in m['stream'] if (e['uid'] in previous) == prior]
        values = [v for v in values if v]
        passed = sum(v['reward'] for v in values)
        groups[label] = {'count': len(values), 'passed': passed,
                         'mean_reward': passed / len(values) if values else None}
    return {**result, 'inference_cohorts': groups,
            'inference_deployment': m['inference_migration']['deployment'],
            'inference_interpretation': m['inference_migration']['interpretation']}


@contextmanager
def adapter(root):
    verify(root)
    with offline.adapter(root):
        previous = v2.verify
        v2.verify = verify
        try:
            yield
        finally:
            v2.verify = previous


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    a = p.add_mutually_exclusive_group(required=True)
    a.add_argument('--prepare-from', type=Path)
    a.add_argument('--run', action='store_true')
    a.add_argument('--report', action='store_true')
    p.add_argument('--deployment', type=Path)
    p.add_argument('--allow-native-runs', action='store_true')
    args = p.parse_args()
    if args.prepare_from:
        if not args.deployment:
            p.error('preparation requires --deployment')
        m = prepare(args.root, args.prepare_from, args.deployment)
        print(json.dumps({'root': str(args.root), 'preserved_official_uids': m['inference_migration']['preserved_official_uids']}))
    elif args.report:
        print(json.dumps(report(args.root, verify(args.root)), indent=2))
    elif not args.allow_native_runs:
        p.error('running requires --allow-native-runs')
    else:
        with adapter(args.root):
            v2.run(args.root)
