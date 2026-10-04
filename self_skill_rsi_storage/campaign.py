"""Continue the same 94 scored tasks after a pre-launch disk-admission failure."""
import argparse
import copy
import json
import os
from pathlib import Path
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_accel.campaign import clone_prefix
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_netguard import campaign as old, runtime as guarded
from self_skill_rsi_network.campaign import copy_frozen
from . import storage, services

base, v2, runtime, runner, POLICY = old.base, old.v2, old.runtime, old.runner, old.POLICY
SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261002-netguard-v1')
PORTS, GROUPS = services.PORTS, services.GROUPS


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def expected_manifest(previous, deployment):
    m = copy.deepcopy(previous)
    m['ports'] = PORTS
    m['inference_migration']['deployment'] = str(deployment)
    return m


def audit_unstarted_validation(stage):
    validation = stage / 'validation'
    if any(validation.rglob('launch.json')) or any(validation.rglob('child.json')) or any(validation.rglob('result.json')):
        raise RuntimeError('validation_already_started_do_not_replay')
    failures = list(validation.glob('*/00/candidate/execution/failure.json'))
    if len(failures) != 2 or any(not read_json(p)['reason'].startswith('disk_admission_failed:') for p in failures):
        raise RuntimeError('expected_two_prelaunch_disk_failures')
    return {str(p): file_hash(p) for p in validation.rglob('*') if p.is_file()}


def prepare(root, deployment):
    root, deployment = root.resolve(), deployment.resolve()
    if root == SOURCE or root in SOURCE.parents or SOURCE in root.parents:
        raise RuntimeError('separate_storage_recovery_root_required')
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('new_recovery_directory_required')
        v2.check_source_stopped(SOURCE)
        previous = old.verify(SOURCE)
        os.environ.update(runner_environment(PORTS[0])); storage.activate()
        disk = storage.disk_status(root)
        if not disk['ok']:
            raise RuntimeError('data2_storage_not_ready:' + str(disk))
        completed = base.completed_prefix(SOURCE, previous, 93)
        before = completed[-1][1]['after']
        if read_json(SOURCE / 'bank/HEAD.json') != before:
            raise RuntimeError('uncommitted_source_bank')
        entry = previous['stream'][93]
        stage = SOURCE / 'stream' / entry['uid']
        if entry['task'] != 'fix-code-vulnerability' or (stage / 'record.json').exists():
            raise RuntimeError('unexpected_storage_boundary')
        scored = read_json(stage / 'scored.json')
        v2.audit_native(entry, scored, 'stream', before)
        update = read_json(stage / 'update/result.json')
        head = read_json(stage / 'candidate/HEAD.json')
        from self_skill_rsi import bank
        payload, provenance = bank.load(stage / 'candidate', head)
        if (update['status'] != 'candidate' or payload != update['payload'] or provenance != update['provenance']
                or head != bank.metadata(payload, provenance, before['revision'] + 1, entry['uid'])):
            raise RuntimeError('pending_candidate_changed')
        frozen = audit_unstarted_validation(stage)
        if any((SOURCE / 'stream' / e['uid']).exists() for e in previous['stream'][94:]):
            raise RuntimeError('unexpected_later_work')
        d = read_json(deployment)
        if (d['ports'] != PORTS or d['gpu_groups'] != GROUPS or d['model'] != previous['model']
                or not d['functional_checks_passed'] or d['generation_configuration_changed']):
            raise RuntimeError('unaccepted_inference_deployment')
        base.verify_files(d['evidence_hashes'])
        frozen.update(clone_prefix(SOURCE, root, completed))
        for name in ('matched-controls.json', 'network-incident.json', 'boundary-admission.json'):
            copy_frozen(SOURCE / name, root / name, frozen)
        for name in ('before.json', 'scored.json', 'source.json', 'validation_cases.json',
                     'validation_references.json', 'parent', 'candidate', 'update', 'solve', 'validation/plan.json'):
            copy_frozen(stage / name, root / 'stream' / entry['uid'] / name, frozen)
        for p in (SOURCE / 'manifest.json', SOURCE / 'seal.json', deployment):
            frozen[str(p)] = file_hash(p)
        frozen.update(d['evidence_hashes'])
        m = expected_manifest(previous, deployment)
        m['storage_migration'] = {'source_root': str(SOURCE), 'created': time.time(),
            'preserved_scored_tasks': 94, 'preserved_complete_updates': 93,
            'pending_candidate_uid': entry['uid'], 'official_replays': 0,
            'candidate_regenerations': 0, 'prelaunch_validation_model_calls': 0,
            'implementation': code(), 'frozen_files': frozen, 'policy': storage.POLICY,
            'runtime_root': str(storage.ROOT), 'disk_acceptance': disk,
            'generation_configuration_changed': False, 'algorithm_changed': False,
            'interpretation': 'Data2 storage recovery, same TP4 model on free GPUs 5-8; '
                'two candidate checks execute sequentially instead of concurrently. '
                'Preserve scores, candidate, task order, skill bank, budgets, tests and update rule.'}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time(), 'completed': 93})
        return verify(root)


def verify(root):
    m, seal = read_json(root / 'manifest.json'), read_json(root / 'seal.json')
    change = m['storage_migration']
    if (seal['manifest_sha256'] != file_hash(root / 'manifest.json') or seal['code'] != v2.snapshot()
            or change['implementation'] != code() or change['policy'] != storage.POLICY):
        raise RuntimeError('storage_recovery_seal_changed')
    v2.check_source_stopped(SOURCE)
    previous = old.verify(SOURCE)
    expected = expected_manifest(previous, Path(m['inference_migration']['deployment']))
    if {k: v for k, v in m.items() if k != 'storage_migration'} != expected:
        raise RuntimeError('unregistered_algorithm_or_protocol_change')
    base.verify_files(change['frozen_files'])
    return m


def report(root, m):
    value = old.report(root, m)
    value['inference'] = {'ports': PORTS, 'gpu_groups': GROUPS, 'candidate_validation_parallelism': 1,
                          'official_stream_parallelism': 1, 'preserved_at_handoff': 94}
    value['storage'] = {'runtime_root': str(storage.ROOT), 'policy': storage.POLICY,
                        'preserved_scored_tasks': 94, 'official_replays': 0}
    return value


def run(root):
    storage.activate()
    saved = runner.verify, runner.report, runner.old.adapter, runtime.native_solve, runtime.propose, base.core.docker_disk_status
    try:
        runner.verify, runner.report, runner.old.adapter = verify, report, old.old.adapter
        runtime.native_solve = lambda *a, **kw: guarded.native(root, *a, **kw)
        runtime.propose = lambda *a, **kw: old.old.reuse_proposal(root, *a, **kw)
        base.core.docker_disk_status = lambda project: storage.disk_status(root)
        runner.run(root)
    finally:
        (runner.verify, runner.report, runner.old.adapter, runtime.native_solve,
         runtime.propose, base.core.docker_disk_status) = saved


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', type=Path, required=True)
    p.add_argument('--prepare', action='store_true'); p.add_argument('--deployment', type=Path)
    p.add_argument('--run', action='store_true'); p.add_argument('--allow-native-runs', action='store_true')
    p.add_argument('--report', action='store_true'); a = p.parse_args(); root = a.root.resolve()
    if a.prepare: prepare(root, a.deployment)
    elif a.report: print(json.dumps(report(root, verify(root)), indent=2))
    elif a.run and a.allow_native_runs: run(root)
    else: p.error('explicit action required')
