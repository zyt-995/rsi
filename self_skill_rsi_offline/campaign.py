"""Preserve all scored work; change only future trials' grading infrastructure."""
import argparse
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import time

from offline_verifier.profiles import sha
from offline_verifier.runner import load_manifest, validate_result
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json
from self_skill_rsi import campaign as base, runtime
from self_skill_rsi_v2 import campaign as v2

ORIGINAL_VERIFY = v2.verify
ORIGINAL_COMMAND = base.core.harbor_command


def code():
    return {str(p.resolve()): sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def stopped(source):
    for name in ('supervisor_launch.json', 'watchdog_launch.json'):
        if base.legacy.alive(read_json(source / name, {})):
            raise RuntimeError('source_controller_is_live')
    for p in source.glob('stream/**/child.json'):
        if base.legacy.alive(read_json(p)):
            raise RuntimeError('source_trial_is_live')


def prepare(root, source, manifest, acceptance):
    root, source = root.resolve(), source.resolve()
    if root == source or root in source.parents or source in root.parents:
        raise ValueError('separate_run_directories_required')
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('nonempty_migration_target')
        stopped(source)
        old = ORIGINAL_VERIFY(source)
        offline = load_manifest(manifest, sha(manifest))
        checks = read_json(acceptance)
        if not isinstance(checks, list) or {r['task'] for r in checks} != {e['task'] for e in old['stream']}:
            raise RuntimeError('offline_acceptance_cohort_incomplete')
        if any(r.get('status') != 'executed' or r.get('network_mode') != 'none' for r in checks):
            raise RuntimeError('offline_acceptance_failed')
        acceptance_files = {}
        for row in checks:
            folder = acceptance.parent / row['task']
            validate_result(folder, row['task'])
            if read_json(folder / 'runtime-provenance.json')['bundle_sha256'] != offline['bundle_sha256']:
                raise RuntimeError('acceptance_was_for_a_different_offline_bundle')
            for p in folder.iterdir():
                if p.is_file():
                    acceptance_files[str(p)] = sha(p)
        for e in old['stream']:
            runtime.check_entry(e)
            cfg = offline['tasks'][e['task']]
            if cfg['task_checksum'] != e['task_checksum'] or cfg['image_id'] != e['image_id']:
                raise RuntimeError('offline_environment_not_registered_for_stream')
        frozen = {str(p): sha(p) for p in (source / 'manifest.json', source / 'seal.json', manifest, acceptance)}
        frozen.update(acceptance_files)
        def copy_file(p, target):
            if not p.exists():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            frozen[str(p)] = sha(p)
        shutil.copytree(source / 'bank', root / 'bank')
        preserved_scores, preserved_validations, superseded_invalid = [], [], []
        for e in old['stream']:
            stage = source / 'stream' / e['uid']
            dest = root / 'stream' / e['uid']
            if not stage.exists():
                continue
            for name in ('before.json', 'scored.json', 'source.json', 'validation_cases.json',
                         'validation_references.json', 'commit_intent.json', 'record.json'):
                copy_file(stage / name, dest / name)
            for directory in ('update', 'parent', 'candidate'):
                for p in (stage / directory).rglob('*'):
                    if p.is_file():
                        copy_file(p, dest / p.relative_to(stage))
            for name in ('plan.json', 'result.json'):
                copy_file(stage / 'validation' / name, dest / 'validation' / name)
            for p in (stage / 'validation').glob('*/*/candidate/record.json'):
                record = read_json(p)
                case = next(x for x in old['stream'] if x['uid'] == record['uid'])
                v2.audit_native(case, record, 'validation', record['before'])
                copy_file(p, dest / p.relative_to(stage))
                preserved_validations.append(str(p))
            if (stage / 'scored.json').exists():
                score = read_json(stage / 'scored.json')
                v2.audit_native(e, score, 'stream', score['before'])
                preserved_scores.append(e['uid'])
            for p in (stage / 'validation').glob('*/*/candidate/trials/*/result.json'):
                if not (p.parent.parent.parent / 'record.json').exists():
                    record = read_json(p)
                    if ((record.get('exception_info') or {}).get('exception_type') != 'VerifierOutputParseError'
                            or record.get('verifier_result') or not (p.parent / 'agent/rsi_end.json').exists()
                            or (p.parent / 'verifier/ctrf.json').exists()):
                        raise RuntimeError('cannot_supersede_an_unreviewed_scored_trial:' + str(p))
                    superseded_invalid.append({'result': str(p), 'sha256': sha(p),
                        'reason': 'grading_infrastructure_failed; no valid score; original artifact retained'})
                    frozen[str(p)] = sha(p)
        # Old completed records refer to their immutable original artifacts.
        completed = sum((root / 'stream' / e['uid'] / 'record.json').exists() for e in old['stream'])
        base.completed_prefix(root, old, completed)
        # Prevent an old online-grading dispatcher from being restarted by accident.
        atomic_json(source / 'PAUSE', {'reason': 'versioned offline grading handoff', 'target': str(root), 'time': time.time()})
        m = copy.deepcopy(old)
        m['offline_migration'] = {'source_root': str(source), 'manifest': str(manifest.resolve()),
            'manifest_sha256': sha(manifest), 'acceptance': str(acceptance.resolve()),
            'created': time.time(), 'preserved_official_uids': preserved_scores,
            'preserved_candidate_validations': preserved_validations,
            'superseded_invalid_trials': superseded_invalid, 'frozen_source_files': frozen,
            'implementation': code(), 'algorithm_changed': False,
            'interpretation': 'offline environment version transition; prior valid scores retained under original environment labels'}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': sha(root / 'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time(),
            'completed': completed, 'scored_tasks': len(preserved_scores), 'offline_verifier': str(manifest),
            'new_model_calls': 0, 'invalid_trial_retry_requires_explicit_run': True})
        return verify(root)


def verify(root):
    m = ORIGINAL_VERIFY(root)
    change = m['offline_migration']
    if change['implementation'] != code():
        raise RuntimeError('offline_campaign_code_changed')
    base.verify_files(change['frozen_source_files'])
    load_manifest(change['manifest'], change['manifest_sha256'])
    stopped(Path(change['source_root']))
    if not (Path(change['source_root']) / 'PAUSE').exists():
        raise RuntimeError('online_source_must_stay_paused')
    return m


@contextmanager
def adapter(root):
    m = verify(root)
    change = m['offline_migration']
    def command(*args, **kwargs):
        return ORIGINAL_COMMAND(*args, **kwargs) + [
            '--verifier', 'offline_verifier.harbor:OfflineVerifier',
            '--verifier-kwarg', 'manifest=' + change['manifest'],
            '--verifier-kwarg', 'manifest_sha256=' + change['manifest_sha256']]
    old_command, old_verify = base.core.harbor_command, v2.verify
    base.core.harbor_command, v2.verify = command, verify
    try:
        yield m
    finally:
        base.core.harbor_command, v2.verify = old_command, old_verify


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    actions = p.add_mutually_exclusive_group(required=True)
    actions.add_argument('--prepare-from', type=Path)
    actions.add_argument('--run', action='store_true')
    actions.add_argument('--report', action='store_true')
    p.add_argument('--manifest', type=Path)
    p.add_argument('--acceptance', type=Path)
    p.add_argument('--allow-native-runs', action='store_true')
    a = p.parse_args()
    if a.prepare_from:
        if a.manifest is None or a.acceptance is None:
            p.error('preparation requires --manifest and --acceptance')
        m = prepare(a.root, a.prepare_from, a.manifest, a.acceptance)
        print(json.dumps(m['offline_migration'], indent=2))
    elif a.report:
        print(json.dumps(v2.report(a.root, verify(a.root)), indent=2))
    elif not a.allow_native_runs:
        p.error('running requires --allow-native-runs')
    else:
        with adapter(a.root):
            v2.run(a.root)
