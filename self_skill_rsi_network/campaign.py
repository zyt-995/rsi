"""Recover an audited outage without rewriting scores or changing the RSI rule."""
import argparse
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import threading
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_resume import campaign as prior
from self_skill_rsi import bank, learning
from . import audit, proxy

base, v2, runtime, POLICY = prior.base, prior.v2, prior.runtime, prior.POLICY
runner = prior.runner
SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261002-resume-v1')
PROXY = Path('/data2/ytzhang/rsi-harness-runs/network-independent-20261002-v1/deployment.json')
ORIGINAL_NATIVE, ORIGINAL_PROPOSE = runtime.native_solve, runtime.propose
ORIGINAL_ADAPTER = runner.old.adapter


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def copy_frozen(src, dst, frozen):
    if src.is_dir():
        shutil.copytree(src, dst)
        for p in src.rglob('*'):
            if p.is_file():
                q = dst / p.relative_to(src)
                if file_hash(p) != file_hash(q): raise RuntimeError('copy_hash_mismatch')
                frozen[str(p)] = frozen[str(q)] = file_hash(p)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        frozen[str(src)] = frozen[str(dst)] = file_hash(src)


def prepare(root):
    root = root.resolve()
    if root == SOURCE or SOURCE in root.parents or root in SOURCE.parents:
        raise RuntimeError('separate_recovery_root_required')
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists(): return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()): raise RuntimeError('target_not_empty')
        v2.check_source_stopped(SOURCE)
        old = prior.verify(SOURCE)
        incidents = audit.inventory(SOURCE)
        positions = [int(x.get('stage_uid', x['uid'])[1:]) for x in incidents]
        if not positions or min(positions) != 39:
            raise RuntimeError('reviewed_recovery_boundary_changed')
        completed = base.completed_prefix(SOURCE, old, 39)
        before = completed[-1][1]['after']
        boundary = SOURCE / 'stream/s000039'
        scored = read_json(boundary / 'scored.json')
        if scored['before'] != before or audit.record_failures(scored):
            raise RuntimeError('boundary_official_score_not_reusable')
        deployment = read_json(PROXY)
        if not deployment['acceptance']['passed'] or not deployment.get('container_acceptance', {}).get('passed'):
            raise RuntimeError('host_and_container_proxy_acceptance_required')
        if not proxy.health()['passed']: raise RuntimeError('independent_proxy_currently_unhealthy')
        frozen = {}
        for e, _ in completed:
            copy_frozen(SOURCE / 'stream' / e['uid'], root / 'stream' / e['uid'], frozen)
        # Branch from the actual pre-incident bank. Never copy future versions.
        for p in sorted((SOURCE / 'bank/versions').iterdir()):
            if p.is_dir() and int(p.name) <= before['revision']:
                copy_frozen(p, root / 'bank/versions' / p.name, frozen)
        atomic_json(root / 'bank/HEAD.json', before)
        bank.load(root / 'bank', before)
        copy_frozen(SOURCE / 'matched-controls.json', root / 'matched-controls.json', frozen)
        # Keep the successful official solve; its existing candidate is reused
        # by exact-input comparison, not regenerated to chase an outcome.
        copy_frozen(boundary / 'scored.json', root / 'stream/s000039/scored.json', frozen)
        for p in [SOURCE / 'manifest.json', SOURCE / 'seal.json', PROXY, Path(deployment['runtime_config'])]:
            frozen[str(p)] = file_hash(p)
        # Freeze the quarantined tail as evidence, although it is not in this cohort.
        for stage in sorted((SOURCE / 'stream').iterdir()):
            if stage.name >= 's000039':
                for p in stage.rglob('*'):
                    if p.is_file(): frozen[str(p)] = file_hash(p)
        atomic_json(root / 'network-incident.json', {'detected': incidents,
            'user_confirmation': 'SSH proxy outage coincides with user computer shutdown; user requested recovery',
            'preserved_official_count': 40, 'preserved_update_count': 39,
            'recovery_boundary': 's000039 candidate validation',
            'reuse_rule': 'Exact same version and native inputs, no observed proxy outage; otherwise recompute dependent tail',
            'previous_scores_overwritten': False,
            'caveat': 'Earlier unrelated network slowness is not retroactively declared a shutdown incident.'})
        frozen[str(root / 'network-incident.json')] = file_hash(root / 'network-incident.json')
        m = copy.deepcopy(old)
        m['network_recovery'] = {'source_root': str(SOURCE), 'created': time.time(),
            'boundary_index': 39, 'preserved_scored_tasks': 40, 'proxy_deployment': str(PROXY),
            'implementation': code(), 'frozen_files': frozen,
            'algorithm_changed': False, 'retry_selection_by_reward': False,
            'inflight_probe_interval_seconds': 30,
            'policy': 'Network failures are infrastructure incidents, never admitted as zero; no automatic trial retry.'}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': v2.snapshot()})
        atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time(), 'completed': 39})
        return verify(root)


def verify(root):
    m, seal = read_json(root / 'manifest.json'), read_json(root / 'seal.json')
    change = m['network_recovery']
    if (seal['manifest_sha256'] != file_hash(root / 'manifest.json') or seal['code'] != v2.snapshot()
            or change['implementation'] != code()): raise RuntimeError('network_recovery_seal_changed')
    v2.check_source_stopped(SOURCE)
    old = prior.verify(SOURCE)
    if {k:v for k,v in m.items() if k != 'network_recovery'} != old:
        raise RuntimeError('network_recovery_changed_algorithm_or_budgets')
    base.verify_files(change['frozen_files'])
    return m


def report(root, m):
    value = prior.report(root, m)
    value['network_recovery'] = {'preserved_scored_tasks': 40, 'boundary_index': 39,
        'independent_proxy': proxy.CONTAINER_URL,
        'incident_report': str(root / 'network-incident.json'),
        'reused_native_records': len(list(root.glob('stream/**/network-reuse.json')))}
    return value


def can_reuse(record, entry, head, purpose):
    return (record and record.get('uid') == entry['uid'] and record.get('task') == entry['task']
            and record.get('before') == head and record.get('purpose') == purpose
            and not audit.record_failures(record))


def guarded_native(root, entry, store, head, folder, port, purpose, *, manifest):
    folder = Path(folder)
    existing = read_json(folder / 'record.json')
    if (folder / 'network-rejected.json').exists():
        raise RuntimeError('network_incident_quarantined_no_automatic_replay')
    if existing:
        v2.gate.validate_record(entry, existing, purpose, head)
        if not ((folder / 'network-accepted.json').exists() or (folder / 'network-reuse.json').exists()):
            raise RuntimeError('native_record_without_completed_network_audit')
        return existing
    old_folder = SOURCE / folder.relative_to(root)
    saved = read_json(old_folder / 'record.json')
    if can_reuse(saved, entry, head, purpose):
        v2.audit_native(entry, saved, purpose, head)
        atomic_json(folder / 'network-reuse.json', {'source_record': str(old_folder / 'record.json'),
            'source_sha256': file_hash(old_folder / 'record.json'), 'new_model_calls': 0,
            'reason': 'identical native inputs and no observed proxy outage'})
        atomic_json(folder / 'record.json', saved)
        return saved
    health = proxy.health()
    atomic_json(folder / 'network/preflight.json', health)
    if not health['passed']: raise RuntimeError('independent_proxy_preflight_failed_no_model_call')
    stop = threading.Event()
    failed = []
    def monitor():
        serial = 0
        while not stop.wait(30):
            check = proxy.health()
            serial += 1
            atomic_json(folder / 'network' / f'probe-{serial:04d}.json', check)
            if not check['passed']: failed.append(check)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        record = ORIGINAL_NATIVE(entry, store, head, folder / 'guarded-execution', port, purpose, manifest=manifest)
    finally:
        stop.set(); thread.join(timeout=20)
    check = proxy.health()
    atomic_json(folder / 'network/postflight.json', check)
    if not check['passed']: failed.append(check)
    trace_failures = audit.record_failures(record)
    if failed or trace_failures:
        atomic_json(folder / 'network-rejected.json', {'failed_probes': failed, 'trace_failures': trace_failures,
            'original_result': record['result'], 'score': None, 'is_algorithmic_zero': False,
            'automatic_replay': False, 'time': time.time()})
        raise RuntimeError('network_incident_not_admitted_as_score')
    atomic_json(folder / 'network-accepted.json', {'passed': True, 'time': time.time(),
        'proxy': proxy.CONTAINER_URL, 'monitored': True, 'network_probe_failures': 0})
    atomic_json(folder / 'record.json', record)
    return record


def reuse_proposal(root, folder, source, old, provenance, history, model, url):
    folder = Path(folder)
    expected = {'source': source, 'old': old, 'provenance': provenance, 'history': history, 'model': model, 'url': url}
    old_folder = SOURCE / folder.relative_to(root)
    if (not folder.exists() and read_json(old_folder / 'input.json') == expected
            and (old_folder / 'result.json').exists()):
        shutil.copytree(old_folder, folder)
        atomic_json(folder.parent / 'proposal-reuse.json', {'source': str(old_folder),
            'reason': 'all immutable proposal inputs identical; preserve original candidate', 'new_model_calls': 0})
    return ORIGINAL_PROPOSE(folder, source, old, provenance, history, model, url)


@contextmanager
def adapter(m):
    with ORIGINAL_ADAPTER(m):
        original = base.core.harbor_command
        def command(*args, **kwargs):
            values = original(*args, **kwargs)
            return [v.replace('=http://10.0.2.2:17897', '=' + proxy.CONTAINER_URL)
                    if v.split('=', 1)[0] in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')
                    else v for v in values]
        base.core.harbor_command = command
        try: yield
        finally: base.core.harbor_command = original


def run(root):
    previous = runner.verify, runner.report, runner.old.adapter, runtime.native_solve, runtime.propose
    try:
        runner.verify, runner.report, runner.old.adapter = verify, report, adapter
        runtime.native_solve = lambda *a, **kw: guarded_native(root, *a, **kw)
        runtime.propose = lambda *a, **kw: reuse_proposal(root, *a, **kw)
        runner.run(root)
    finally:
        runner.verify, runner.report, runner.old.adapter, runtime.native_solve, runtime.propose = previous


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path)
    p.add_argument('--prepare', action='store_true'); p.add_argument('--run', action='store_true')
    p.add_argument('--report', action='store_true'); p.add_argument('--allow-native-runs', action='store_true')
    a = p.parse_args(); root = a.root.resolve()
    if a.prepare: prepare(root)
    elif a.report: print(json.dumps(report(root, verify(root)), indent=2))
    elif a.run and a.allow_native_runs: run(root)
    else: p.error('explicit action required')
