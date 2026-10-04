"""Seal an infrastructure-only amendment with preserved historical evidence.

Reuses the existing real-model acceptance ONLY because model/solver/updater/
retriever/consolidator code and budgets are byte-identical. New operational
validation/recovery/monitoring is covered by regression tests and artifact audit.
"""
import argparse
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cross_chain_rsi import campaign as c, admission, memory
from rsi_harness.clean_rsi import read_json, digest
from rsi_harness.terminal_infra import atomic_json, file_hash


def amend(root, tests):
    pending = read_json(root / 'pending_operational_amendment.json')
    audit = Path(pending['audit'])
    with c.legacy.exclusive(root / 'operational-amendment.lock'), c.legacy.exclusive(root / 'scheduler.lock'), c.legacy.exclusive(c.SOURCE_ROOT / 'scheduler.lock'):
        before = read_json(audit / 'seal.json.before')
        current = read_json(root / 'seal.json')
        if current != before:
            c.verify(root)
            return {'status': 'already_amended'}
        if read_json(root / 'manifest.json') != read_json(audit / 'manifest.json.before'):
            raise RuntimeError('protocol changed beyond operations')
        for p in (root / 'supervisor_launch.json', root / 'watchdog_launch.json'):
            launch = read_json(p)
            if launch and c.legacy.alive(launch):
                raise RuntimeError('operational process still live')
        manifest = read_json(root / 'manifest.json')
        if c.recovery.adopt_untracked(root, manifest, read_json(root / 'active.json', {})):
            raise RuntimeError('workers still present; do not amend a live campaign')
        snapshot = c.code_snapshot()
        changed = {p for p in set(before['code']) | set(snapshot) if before['code'].get(p) != snapshot.get(p)}
        allowed = {str(c.PROJECT / 'cross_chain_rsi' / name) for name in ('campaign.py', 'admission.py', 'recovery.py', 'watchdog.py')}
        if changed != allowed:
            raise RuntimeError('unexpected code changes:' + str(sorted(changed ^ allowed)))
        c.legacy.verify(c.SOURCE_ROOT)
        preserved = read_json(audit / 'completed.before.json')
        if any(file_hash(Path(p)) != h for p, h in preserved.items()):
            raise RuntimeError('previous completed result changed')
        suites = ET.parse(tests).getroot()
        rows = list(suites.iter('testsuite'))
        counts = {k: sum(int(row.get(k, '0')) for row in rows) for k in ('tests', 'failures', 'errors', 'skipped')}
        if counts['tests'] < 180 or counts['failures'] or counts['errors']:
            raise RuntimeError('full regression suite must pass')
        acceptance = Path(before['acceptance'])
        if file_hash(acceptance) != before['acceptance_sha256']:
            raise RuntimeError('original acceptance changed')
        smoke = read_json(acceptance)
        if not smoke['passed'] or smoke['formal'] is not False:
            raise RuntimeError('original real-model acceptance is invalid')
        for result in smoke['results']:
            if not admission.scored_result(Path(result['result']))[0] or result['reward'] != 1:
                raise RuntimeError('reused smoke artifact not valid')
        for arm in memory.ARMS:
            memory.verify_frozen(acceptance.parent / 'banks' / arm)
        task = 'break-filter-js-from-html-EVO-3'
        folder = c.legacy.stage_path(c.SOURCE_ROOT, 'break-filter-js-from-html', task)
        trial = next((folder / 'trials').glob('*/result.json'))
        decision = admission.assess(trial)
        if not decision['valid'] or not decision['override'] or admission.compatibility(trial, task, None):
            raise RuntimeError('blocked source lacks valid recovery evidence')
        report = {'time': time.time(), 'scope': pending['scope'], 'changed_files': sorted(changed),
                  'code_sha256': digest(snapshot), 'manifest_unchanged': True,
                  'historical_solver_updater_and_consolidation_unchanged': True,
                  'legacy_source_seal_unchanged': True, 'completed_records_preserved': len(preserved),
                  'tests': str(tests), 'tests_sha256': file_hash(tests), 'test_counts': counts,
                  'reused_real_model_acceptance': str(acceptance), 'real_model_calls_for_this_amendment': 0,
                  'reuse_reason': 'only result admissibility and controller recovery changed; original real-model paths and frozen bank checks revalidated',
                  'blocked_source_admission': decision,
                  'policy': 'curl-only recovered cache bootstrap; complete consistent CTRF/reward; each failed test must directly prove an explicitly required artifact missing; no hidden grader text sent to learning',
                  'no_solver_replay': True}
        atomic_json(audit / 'validation.json', report)
        updated = {**before, 'code': snapshot, 'time': time.time(), 'operational_amendment': {
            'report': str(audit / 'validation.json'), 'sha256': file_hash(audit / 'validation.json'),
            'previous_seal': str(audit / 'seal.json.before'), 'previous_seal_sha256': file_hash(audit / 'seal.json.before')}}
        atomic_json(root / 'seal.json', updated)
        atomic_json(audit / 'seal.json.after', updated)
        c.verify(root)
        return {'status': 'amended', 'audit': str(audit), 'completed_preserved': len(preserved), 'tests': counts}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--tests', type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(amend(args.root.resolve(), args.tests.resolve()), ensure_ascii=False, indent=2))
