"""Audited, narrow recovery from obsolete bootstrap-log false positives.

No grader reruns, changed rewards, or hidden-test information in learning inputs.
Old frozen code is unchanged. A scoped compatibility adapter is used ONLY in
the registered cross-chain source workers and source compatibility checks.
"""
from __future__ import annotations

import math
import re
from contextlib import contextmanager
from pathlib import Path

from rsi_harness import clean_campaign as core
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash

ORIGINAL_SCORED_RESULT = core.scored_result
MARKERS = ('curl: command not found', 'uvx: command not found', 'No space left on device',
           'Could not resolve host', 'Connection refused', 'Unable to locate package',
           'Failed to establish a new connection', 'Network is unreachable')


def assess(path):
    """Accept existing normal results unchanged; narrowly prove a recovered case."""
    path = Path(path)
    valid, reason = ORIGINAL_SCORED_RESULT(path)
    verdict = {'valid': valid, 'legacy_reason': reason, 'override': False}
    if valid or not str(reason).startswith('verifier_infrastructure:'):
        return verdict
    value = read_json(path, {})
    if value.get('exception_info'):
        return verdict
    reward = (value.get('verifier_result') or {}).get('rewards', {}).get('reward')
    if type(reward) not in (int, float) or not math.isfinite(reward) or reward not in (0, 1):
        return verdict
    log = path.parent / 'verifier/test-stdout.txt'
    text = log.read_text(errors='replace')
    boundary = re.search(r'^=+ test session starts =+\s*$', text, re.MULTILINE)
    if not boundary:
        return verdict
    prelude, tests_text = text[:boundary.start()], text[boundary.end():]
    # Only the known curl bootstrap may be waived, not disk/network/runtime errors.
    if any(marker in tests_text for marker in MARKERS):
        return verdict
    if any(marker in prelude for marker in MARKERS if marker not in ('Unable to locate package', 'curl: command not found')):
        return verdict
    package_errors = [line.strip() for line in prelude.splitlines() if 'Unable to locate package' in line]
    if any(line != 'E: Unable to locate package curl' for line in package_errors):
        return verdict
    if 'Installed locally cached uv ' not in prelude:
        return verdict
    report_path = path.parent / 'verifier/ctrf.json'
    report = read_json(report_path, {}).get('results', {})
    summary, rows = report.get('summary', {}), report.get('tests', [])
    fields = ('tests', 'passed', 'failed', 'skipped', 'pending', 'other')
    if any(type(summary.get(k)) is not int or summary[k] < 0 for k in fields):
        return verdict
    if (not summary['tests'] or summary['tests'] != len(rows) or summary['skipped']
            or summary['pending'] or summary['other'] or summary['passed'] + summary['failed'] != summary['tests']):
        return verdict
    if sum(r.get('status') == 'passed' for r in rows) != summary['passed'] or sum(r.get('status') == 'failed' for r in rows) != summary['failed']:
        return verdict
    if reward != (1 if not summary['failed'] else 0):
        return verdict
    reward_file = path.parent / 'verifier/reward.txt'
    try:
        if float(reward_file.read_text().strip()) != reward:
            return verdict
    except (OSError, ValueError):
        return verdict
    # The grader's fail must directly witness a missing explicit output contract.
    # Arbitrary AssertionErrors (e.g. an assertion on a failed dependency import)
    # are NOT sufficient to waive an infrastructure warning.
    task_path = Path(str((value.get('config', {}).get('task') or {}).get('path', ''))).resolve()
    if task_path.parent != core.DATASET.resolve():
        return verdict
    instruction_path = task_path / 'instruction.md'
    instruction = instruction_path.read_text()
    missing = []
    for row in rows:
        if row['status'] == 'passed':
            continue
        trace = row.get('trace', '')
        match = re.search(r'^E\s+AssertionError: File (/[^\s]+) does not exist\s*$', trace, re.MULTILINE)
        if row.get('raw_status') != 'call_failed' or not match or not trace.rstrip().endswith(': AssertionError'):
            return verdict
        artifact = match.group(1)
        if not re.search(r'(?<![\w./-])' + re.escape(artifact) + r'(?![\w./-])', instruction):
            return verdict
        if '.exists()' not in trace or f"PosixPath('{artifact}').exists" not in trace or not re.search(r'^E\s+assert False\s*$', trace, re.MULTILINE):
            return verdict
        missing.append({'test': row['name'], 'required_output': artifact})
    paths = [path, report_path, log, reward_file, instruction_path]
    return {**verdict, 'valid': True, 'override': True,
            'reason': 'recovered_cached_bootstrap_with_decisive_completed_tests',
            'raw_reward_unchanged': reward, 'missing_required_outputs': missing,
            'all_tests_passed': not bool(missing), 'hashes': {str(p): file_hash(p) for p in paths},
            'scope': 'host-only result admissibility audit; NEVER supplied to a learner'}


def scored_result(path, audit_root=None):
    verdict = assess(path)
    if verdict['override'] and audit_root is not None:
        target = Path(audit_root) / 'result_admissions' / (file_hash(Path(path)) + '.json')
        old = read_json(target)
        if old is not None and old != verdict:
            raise RuntimeError('admission evidence changed')
        if old is None:
            atomic_json(target, verdict)
    return verdict['valid'], None if verdict['valid'] else verdict['legacy_reason']


@contextmanager
def legacy_adapter(audit_root):
    """Single worker process only; no persistent edits to sealed legacy modules."""
    previous = core.scored_result
    core.scored_result = lambda path: scored_result(path, audit_root)
    try:
        yield
    finally:
        core.scored_result = previous


def compatibility(path, task, audit_root):
    with legacy_adapter(audit_root):
        return core.baseline_compatibility(path, task)
