"""Re-admit one audited native result and continue without rerunning that task."""
import argparse
import copy
from pathlib import Path
import json
import os
import shutil
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_accel.campaign import clone_prefix
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_network import campaign as old
from . import health, runtime as guarded

base, v2, runtime, POLICY = old.base, old.v2, old.runtime, old.POLICY
runner = old.runner
SOURCE = guarded.SOURCE
PORTS, GROUPS = old.prior.PORTS, old.prior.GROUPS


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def expected_manifest(previous, deployment):
    value = copy.deepcopy(previous)
    value['inference_migration']['deployment'] = str(deployment)
    return value


def prepare(root, deployment):
    root, deployment = root.resolve(), deployment.resolve()
    if root == SOURCE or root in SOURCE.parents or SOURCE in root.parents:
        raise RuntimeError('separate_admission_root_required')
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists(): return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()): raise RuntimeError('target_not_empty')
        v2.check_source_stopped(SOURCE)
        previous = old.verify(SOURCE)
        os.environ.update(runner_environment(previous['ports'][0]))
        completed = base.completed_prefix(SOURCE, previous, 42)
        before = completed[-1][1]['after']
        if read_json(SOURCE / 'bank/HEAD.json') != before: raise RuntimeError('uncommitted_source_bank')
        entry = previous['stream'][42]
        stage = SOURCE / 'stream' / entry['uid']
        if entry['task'] != 'compile-compcert-EVO-2' or (stage / 'scored.json').exists():
            raise RuntimeError('unexpected_boundary')
        raw_path = stage / 'solve/guarded-execution/record.json'
        record = read_json(raw_path)
        v2.audit_native(entry, record, 'stream', before)
        probes = [read_json(p) for p in sorted((stage / 'solve/network').glob('*.json'))]
        trajectory = Path(record['result']).parent / 'agent/trajectory.json'
        assessment = health.decide(trajectory, probes)
        if not assessment['accepted'] or not assessment['positive_network_evidence']:
            raise RuntimeError('boundary_evidence_does_not_support_admission')
        for e in previous['stream'][43:]:
            if (SOURCE / 'stream' / e['uid']).exists(): raise RuntimeError('unexpected_later_source_work')
        d = read_json(deployment)
        if (d['ports'] != PORTS or d['gpu_groups'] != GROUPS or d['model'] != previous['model']
                or not d['functional_checks_passed']): raise RuntimeError('unaccepted_inference_deployment')
        base.verify_files(d['evidence_hashes'])
        frozen = clone_prefix(SOURCE, root, completed)
        old.copy_frozen(SOURCE / 'matched-controls.json', root / 'matched-controls.json', frozen)
        old.copy_frozen(SOURCE / 'network-incident.json', root / 'network-incident.json', frozen)
        # This is the original completed solve, not a fresh run or changed score.
        old.copy_frozen(raw_path, root / 'stream' / entry['uid'] / 'scored.json', frozen)
        assessment.update(original_record=str(raw_path), original_result=record['result'],
            original_rejection=str(stage / 'solve/network-rejected.json'), new_model_calls=0,
            original_reward_preserved=True,
            reviewed_explanation='APT download and installation succeeded; mistaken repository returned 404; '
                'correct CompCert archive returned HTTP 200 at the final turn; no task transport error observed.')
        atomic_json(root / 'boundary-admission.json', assessment)
        for p in list(stage.rglob('*')) + [SOURCE / 'manifest.json', SOURCE / 'seal.json', deployment,
                                          root / 'boundary-admission.json']:
            if p.is_file(): frozen[str(p)] = file_hash(p)
        frozen.update(d['evidence_hashes'])
        m = expected_manifest(previous, deployment)
        m['network_admission_migration'] = {'source_root': str(SOURCE), 'created': time.time(),
            'preserved_complete_steps': 42, 'audited_native_uid': entry['uid'], 'official_solves_replayed': 0,
            'implementation': code(), 'frozen_files': frozen, 'policy': health.POLICY,
            'algorithm_changed': False, 'score_selection_by_reward': False,
            'interpretation': 'Network admission correction, original native score preserved. '
                'No changes to model, task order, skills, update rule, official tests or solve budgets.'}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time(), 'completed': 42})
        return verify(root)


def verify(root):
    m, seal = read_json(root / 'manifest.json'), read_json(root / 'seal.json')
    change = m['network_admission_migration']
    if (seal['manifest_sha256'] != file_hash(root / 'manifest.json') or seal['code'] != v2.snapshot()
            or change['implementation'] != code() or change['policy'] != health.POLICY):
        raise RuntimeError('network_admission_seal_changed')
    v2.check_source_stopped(SOURCE)
    previous = old.verify(SOURCE)
    expected = expected_manifest(previous, Path(m['inference_migration']['deployment']))
    if {k:v for k,v in m.items() if k != 'network_admission_migration'} != expected:
        raise RuntimeError('unregistered_algorithm_or_protocol_change')
    base.verify_files(change['frozen_files'])
    return m


def report(root, m):
    value = old.report(root, m)
    value['network_admission'] = {'boundary_readmitted_without_replay': 's000042',
        'policy': health.POLICY, 'audit': str(root / 'boundary-admission.json')}
    return value


def run(root):
    saved = runner.verify, runner.report, runner.old.adapter, runtime.native_solve, runtime.propose
    try:
        runner.verify, runner.report, runner.old.adapter = verify, report, old.adapter
        runtime.native_solve = lambda *a, **kw: guarded.native(root, *a, **kw)
        runtime.propose = lambda *a, **kw: old.reuse_proposal(root, *a, **kw)
        runner.run(root)
    finally:
        runner.verify, runner.report, runner.old.adapter, runtime.native_solve, runtime.propose = saved


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', type=Path, required=True)
    p.add_argument('--prepare', action='store_true'); p.add_argument('--deployment', type=Path)
    p.add_argument('--run', action='store_true'); p.add_argument('--allow-native-runs', action='store_true')
    p.add_argument('--report', action='store_true'); a = p.parse_args(); root = a.root.resolve()
    if a.prepare: prepare(root, a.deployment)
    elif a.report: print(json.dumps(report(root, verify(root)), indent=2))
    elif a.run and a.allow_native_runs: run(root)
    else: p.error('explicit action required')
