"""Source-only consolidation followed by paired, frozen-memory target evaluation.

No dependency on edits to the already sealed chain-local algorithm. Existing
source workers are adopted, not restarted. Formal target outputs never train.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from rsi_harness import clean_campaign as core, meta_skill_campaign as legacy
from rsi_harness.clean_rsi import SOLVE, digest, read_json
from rsi_harness.terminal_infra import atomic_json, file_hash, fingerprint, readiness
from . import memory, admission, recovery

PROJECT, DATASET = core.PROJECT, core.DATASET
SOURCE_ROOT = Path('/data2/ytzhang/rsi-harness-runs/meta-skill-20260929-qwen38-27b')
SOURCES = ('break-filter-js-from-html', 'chess-best-move', 'bn-fit-modify', 'build-cython-ext')
TARGETS = ('multi-source-data-merger', 'torch-pipeline-parallelism', 'winning-avg-corewars', 'large-scale-text-editing')
ARMS = ('baseline', *memory.ARMS)
PORTS = (8040, 8041, 8042, 8044)
VERIFIER_TIMEOUT = 1800


def code_snapshot():
    value = legacy.code_snapshot()
    paths = list((PROJECT / 'cross_chain_rsi').glob('*.py'))
    paths += [DATASET.parent / f'harbor_EvoMem/scripts/terminus2_llm_{p}.env' for p in PORTS]
    return {**value, **{str(p): file_hash(p) for p in sorted(paths)}}


def record_path(root, chain, arm, task):
    return root / chain / arm / 'stages' / task / 'record.json'


def source_entries(manifest, arm):
    root = Path(manifest['source_roots'][arm])
    return [{'chain': c, 'store': str(root / c / arm / 'state'), 'tasks': [
        {'task': t, 'record': str(record_path(root, c, arm, t)), 'instruction': str(DATASET / t / 'instruction.md')}
        for t in manifest['source_tasks'][c]]} for c in manifest['source_order']]


def source_progress(manifest, arm):
    complete, blocked, pending = [], [], []
    for entry in source_entries(manifest, arm):
        for row in entry['tasks']:
            record = read_json(Path(row['record']), {})
            item = {'chain': entry['chain'], 'task': row['task']}
            if record.get('status') == 'complete':
                complete.append(item)
            elif record:
                blocked.append({**item, 'reason': record.get('reason', record.get('status'))})
                break
            else:
                pending.append(item)
                break
    return {'complete': complete, 'blocked': blocked, 'next': pending}


def image_id(image):
    r = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                       capture_output=True, text=True, timeout=15, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def prebuilt_asset_exemption(task, ticket):
    """Only one known prebuilt-only asset, cryptographically matched to its LFS OID.

    Never bypass test assets, build contexts, or unrelated LFS pointers. Harbor
    must use the audited prebuilt image; building from this host context is invalid.
    """
    if task != 'multi-source-data-merger':
        raise RuntimeError('no authorized prebuilt asset exemption:' + task)
    cfg = tomllib.loads((DATASET / task / 'task.toml').read_text())
    if cfg['environment'].get('docker_image') != ticket['image']:
        raise RuntimeError('asset exemption requires an explicit prebuilt image')
    relative, inside = 'environment/data/source_c/users.parquet', '/data/source_c/users.parquet'
    if core.missing_assets(DATASET / task) != [relative]:
        raise RuntimeError('unexpected missing assets')
    pointer = DATASET / task / relative
    lines = pointer.read_text().splitlines()
    expected_hash = next(x.split(':', 1)[1] for x in lines if x.startswith('oid sha256:'))
    expected_size = int(next(x.split()[1] for x in lines if x.startswith('size ')))
    command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
               '--pids-limit', '32', '--memory', '128m', '--cpus', '.25', '--entrypoint', '/bin/sh',
               ticket['image_id'], '-c', f'sha256sum {inside}; wc -c <{inside}']
    r = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
    fields = r.stdout.split()
    if fields != [expected_hash, inside, str(expected_size)]:
        raise RuntimeError('prebuilt asset differs from LFS OID/size')
    return {relative: {'host_pointer_sha256': file_hash(pointer), 'sha256': expected_hash,
                       'size': expected_size, 'image_id': ticket['image_id'], 'image_path': inside}}


def target_ready(root, task, manifest):
    ticket = readiness(root, task)
    if ticket.get('status') != 'ready':
        return False, 'preflight:' + str(ticket.get('status'))
    if ticket['image_id'] != manifest['target_images'][task]['image_id'] or image_id(ticket['image']) != ticket['image_id']:
        return False, 'image_changed'
    exemptions = manifest['asset_exemptions'].get(task, {})
    for relative in core.missing_assets(DATASET / task):
        row = exemptions.get(relative, {})
        if (not relative.startswith('environment/') or row.get('image_id') != ticket['image_id']
                or row.get('host_pointer_sha256') != file_hash(DATASET / task / relative)):
            return False, 'unmaterialized_lfs:' + relative
    return True, None


def exposure_audit(target_tasks):
    """Conservative filesystem evidence, not a claim about model pretraining."""
    hits, examined = [], 0
    roots = [PROJECT / 'runs', SOURCE_ROOT.parent]
    for root in roots:
        for path in root.rglob('*.json'):
            if path.name not in ('result.json', 'launch.json') or '_infra' in path.parts:
                continue
            if any('preflight' in part for part in path.parts):
                continue
            examined += 1
            value = read_json(path, {})
            task = value.get('task_name') or value.get('task')
            cfg = value.get('config') or {}
            if not task and isinstance(cfg.get('task'), dict):
                task = Path(str(cfg['task'].get('path', ''))).name
            command = value.get('command', [])
            if not task and isinstance(command, list) and '--path' in command:
                task = Path(command[command.index('--path') + 1]).name
            if task in target_tasks:
                hits.append({'task': task, 'path': str(path), 'sha256': file_hash(path)})
    return {'time': time.time(), 'searched_roots': list(map(str, roots)), 'examined': examined,
            'hits': hits, 'scope': 'local persisted solver attempts; not a pretraining or off-machine exposure guarantee'}


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    with legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return read_json(root / 'manifest.json')
        old = legacy.verify(SOURCE_ROOT)
        source_tasks = {c: old['task_order'][c] for c in SOURCES}
        target_tasks = {c: core.tasks(c) for c in TARGETS}
        audit = exposure_audit({t for v in target_tasks.values() for t in v})
        atomic_json(root / 'target_exposure_audit.json', audit)
        if audit['hits']:
            raise RuntimeError('target cohort has prior local solver attempts; explicit review required')
        tickets, exemptions = {}, {}
        for task in [t for v in target_tasks.values() for t in v]:
            ticket = readiness(root, task)
            if ticket.get('status') == 'missing':
                ticket = readiness(core.OLD_ROOT, task)
                if ticket.get('status') == 'ready':
                    atomic_json(root / '_infra/readiness' / f'{task}.json', ticket)
            if ticket.get('status') == 'ready':
                if image_id(ticket['image']) != ticket['image_id']:
                    raise RuntimeError('target image changed:' + task)
                tickets[task] = ticket
            else:
                # Register a candidate's cached image WITHOUT certifying its grader.
                # Source workers need not idle while independent preflight runs.
                runtime_paths = list(Path(ticket.get('output', '/nonexistent')).glob('*/agent/infrastructure.json'))
                if len(runtime_paths) != 1:
                    raise RuntimeError('cannot pin pending target image:' + task)
                runtime = read_json(runtime_paths[0])
                cached_id = image_id(runtime['image'])
                if not cached_id or runtime.get('tmux') != 'passed':
                    raise RuntimeError('pending target image unavailable:' + task)
                tickets[task] = {'image': runtime['image'], 'image_id': cached_id,
                                 'status': 'pending_preflight', 'evidence': str(runtime_paths[0]),
                                 'evidence_sha256': file_hash(runtime_paths[0])}
            if core.missing_assets(DATASET / task):
                exemptions[task] = prebuilt_asset_exemption(task, tickets[task])
        from harbor.models.task.task import Task
        all_targets = list(tickets)
        manifest = {'schema': 'cross-chain-rsi-v1', 'created': time.time(), 'benchmark': 'Terminal-Bench-Evo',
                    'model': core.MODEL, 'source_order': list(SOURCES), 'source_tasks': source_tasks,
                    'source_roots': {'skill-rsi': str(legacy.CONTROL), 'meta-skill-rsi': str(SOURCE_ROOT)},
                    'target_order': list(TARGETS), 'target_tasks': target_tasks, 'arms': list(ARMS),
                    'ports': list(PORTS), 'concurrency': 4, 'solve': SOLVE, 'verifier_timeout': VERIFIER_TIMEOUT,
                    'consolidation': memory.BUDGET, 'retrieval': {'top_k': 4, 'chars': 12000},
                    'memory_protocol': 'independent chain-local source V0; batch consolidate own sources into separate V1 banks; all target tasks use immutable V1; NO target updates',
                    'selection': 'unattempted local chains with reusable verified environments, category diversity; not by rewards',
                    'interpretation': 'source-to-target frozen transfer pilot; historical source learning uses unequal compute; NOT online cross-chain training or equal-total-compute comparison',
                    'target_images': tickets, 'asset_exemptions': exemptions,
                    'task_checksums': {t: Task(DATASET / t).checksum for t in all_targets},
                    'environment_fingerprints': {t: fingerprint(DATASET / t) for t in all_targets},
                    'exposure_audit_sha256': file_hash(root / 'target_exposure_audit.json'),
                    'source_manifest_sha256': file_hash(SOURCE_ROOT / 'manifest.json')}
        # Ordinary source controls must be valid and complete before registration.
        for entry in source_entries(manifest, 'skill-rsi'):
            for row in entry['tasks']:
                record = read_json(Path(row['record']), {})
                if record.get('status') != 'complete' or core.baseline_compatibility(Path(record['result']), row['task']):
                    raise RuntimeError('invalid historical ordinary source:' + row['task'])
        atomic_json(root / 'manifest.json', manifest)
        return manifest


def seal(root, acceptance):
    report = read_json(acceptance / 'acceptance.json', {})
    current = code_snapshot()
    if not report.get('passed') or report.get('formal') is not False or report.get('code_sha256') != digest(current):
        raise RuntimeError('independent current-code acceptance is required')
    value = {'manifest_sha256': file_hash(root / 'manifest.json'), 'code': current,
             'acceptance': str(acceptance / 'acceptance.json'),
             'acceptance_sha256': file_hash(acceptance / 'acceptance.json'), 'time': time.time()}
    if (root / 'seal.json').exists():
        verify(root)
    else:
        atomic_json(root / 'seal.json', value)


def verify(root):
    value = read_json(root / 'seal.json', {})
    if value.get('manifest_sha256') != file_hash(root / 'manifest.json') or value.get('code') != code_snapshot():
        raise RuntimeError('missing seal or frozen code/protocol changed')
    if file_hash(Path(value['acceptance'])) != value['acceptance_sha256']:
        raise RuntimeError('acceptance changed')
    amendment = value.get('operational_amendment')
    if amendment and file_hash(Path(amendment['report'])) != amendment['sha256']:
        raise RuntimeError('operational amendment audit changed')
    m = read_json(root / 'manifest.json')
    if file_hash(root / 'target_exposure_audit.json') != m['exposure_audit_sha256']:
        raise RuntimeError('exposure audit changed')
    if file_hash(SOURCE_ROOT / 'manifest.json') != m['source_manifest_sha256']:
        raise RuntimeError('source protocol changed')
    legacy.verify(SOURCE_ROOT)
    return m


def merge(root, arm, port):
    m = verify(root)
    if arm not in memory.ARMS or port not in m['ports']:
        raise ValueError('unregistered merge')
    # Both source cohorts must be complete; no target calls before both banks freeze.
    if any(source_progress(m, a)['next'] or source_progress(m, a)['blocked'] for a in memory.ARMS):
        raise RuntimeError('source phase incomplete')
    with legacy.exclusive(PROJECT / 'runs' / f'meta-port-{port}.lock'):
        for entry in source_entries(m, arm):
            for row in entry['tasks']:
                legacy.verify_task(SOURCE_ROOT, read_json(SOURCE_ROOT / 'manifest.json'), row['task'])
                record = read_json(Path(row['record']))
                if admission.compatibility(Path(record['result']), row['task'], root):
                    raise RuntimeError('source solve compatibility changed')
        source, audit = memory.collect_sources(source_entries(m, arm))
        bank = root / 'banks' / arm
        prior = read_json(bank / 'source_audit.json')
        if prior is not None and prior != audit:
            raise RuntimeError('source audit changed')
        atomic_json(bank / 'source_audit.json', audit)
        result = memory.consolidate(bank, arm, source, f'http://127.0.0.1:{port}/v1')
        if result['reason'] not in ('accepted', 'recovered_commit', 'no_change_or_all_rejected'):
            raise RuntimeError('consolidation technical failure; do not silently evaluate an empty bank:' + result['reason'])
        if any(file_hash(Path(p)) != h for p, h in audit.items()):
            raise RuntimeError('source changed during merge')
        memory.freeze(bank)


def banks_frozen(root):
    for arm in memory.ARMS:
        if not (root / 'banks' / arm / 'frozen.json').exists():
            return False
        memory.verify_frozen(root / 'banks' / arm)
    return True


def execute_trial(folder, task_path, arm, store, port, expected, smoke=False):
    """One native solve. Reuse a saved result; never resample an interrupted solve."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = list((folder / 'trials').glob('*/result.json'))
    if not paths:
        if (folder / 'launch.json').exists():
            raise RuntimeError('interrupted_trial_not_repeated')
        command = core.harbor_command(task_path, folder / 'trials', arm, store)
        command[command.index('--verifier-timeout') + 1] = str(VERIFIER_TIMEOUT)
        if smoke:
            command = [s.replace('"max_tokens": 32768', '"max_tokens": 4096').replace('"timeout": 2700', '"timeout": 600')
                       .replace('max_turns=15', 'max_turns=5') for s in command]
            command[command.index('--agent-timeout') + 1] = '900'
        atomic_json(folder / 'launch.json', {'command': command, 'task': task_path.name, 'port': port,
                    'before': expected, 'started': time.time(), 'formal': not smoke})
        with (folder / 'worker.log').open('ab', buffering=0) as out:
            process = subprocess.Popen(command, cwd=PROJECT, env=core.runner_environment(port), stdout=out, stderr=subprocess.STDOUT)
            atomic_json(folder / 'child.json', {'pid': process.pid, 'identity': core.process_identity(process.pid)})
            try:
                process.wait(timeout=1800 if smoke else 10800)
            except subprocess.TimeoutExpired:
                raise RuntimeError('harbor_outer_watchdog; child retained for inspection')
        paths = list((folder / 'trials').glob('*/result.json'))
    if len(paths) != 1:
        raise RuntimeError('missing_or_ambiguous_trial')
    path = paths[0]
    valid, reason = admission.scored_result(path, folder)
    if not valid:
        raise RuntimeError('invalid_result:' + str(reason))
    start = read_json(path.parent / 'agent/rsi_start.json', {})
    runtime = read_json(path.parent / 'agent/rsi_end.json', {})
    if not start or start.get('version') != expected or start.get('arm') != arm:
        raise RuntimeError('solver_did_not_consume_expected_version')
    if runtime.get('model_calls', 0) > SOLVE['max_model_calls'] or runtime.get('commands', 0) > SOLVE['max_commands']:
        raise RuntimeError('solver_budget_violation')
    value = read_json(path)
    return {'status': 'complete', 'task': task_path.name, 'result': str(path),
            'reward': value['verifier_result']['rewards']['reward'], 'before': expected,
            'runtime': runtime, 'selected_skills': start.get('selected_skills', []),
            'solve_seconds': path.stat().st_mtime - read_json(folder / 'launch.json')['started'],
            'solve_usage': {k: (value.get('agent_result') or {}).get(k) for k in ('n_input_tokens', 'n_output_tokens', 'n_cache_tokens')},
            'target_learning': False, 'finished': time.time()}


def stage(root, chain, arm, task, port):
    m = verify(root)
    if arm not in ARMS or port not in m['ports'] or task not in m['target_tasks'][chain]:
        raise ValueError('work outside frozen manifest')
    with legacy.exclusive(root / chain / arm / 'worker.lock'), legacy.exclusive(PROJECT / 'runs' / f'meta-port-{port}.lock'):
        path = record_path(root, chain, arm, task)
        if path.exists():
            return
        if not banks_frozen(root):
            raise RuntimeError('target phase requires BOTH frozen banks')
        series = m['target_tasks'][chain]
        if any(read_json(record_path(root, chain, arm, t), {}).get('status') != 'complete' for t in series[:series.index(task)]):
            raise RuntimeError('chain predecessor incomplete')
        from harbor.models.task.task import Task
        if Task(DATASET / task).checksum != m['task_checksums'][task] or fingerprint(DATASET / task) != m['environment_fingerprints'][task]:
            raise RuntimeError('target task changed')
        for check_task in (series if task == series[0] else [task]):
            ok, reason = target_ready(root, check_task, m)
            if not ok:
                raise RuntimeError('target environment:' + check_task + ':' + str(reason))
        bank = root / 'banks' / arm
        frozen = memory.verify_frozen(bank) if arm != 'baseline' else None
        expected = frozen['version'] if frozen else None
        record = execute_trial(path.parent, DATASET / task, 'baseline' if arm == 'baseline' else 'skill-rsi', bank / 'state', port, expected)
        if frozen and memory.verify_frozen(bank) != frozen:
            raise RuntimeError('frozen target memory changed')
        atomic_json(path, {**record, 'arm': arm, 'chain': chain, 'bank': frozen, 'readiness': readiness(root, task)})


def source_stage(root, chain, task, port):
    m = verify(root)
    if chain not in m['source_order'] or task not in m['source_tasks'][chain] or port not in m['ports']:
        raise ValueError('source work outside registered cohort')
    with admission.legacy_adapter(root):
        legacy.run_stage(SOURCE_ROOT, chain, task, port)


def target_scan(root, m):
    done, blocked, candidates = {}, [], []
    for arm in ARMS:
        for c in m['target_order']:
            for index, t in enumerate(m['target_tasks'][c]):
                r = read_json(record_path(root, c, arm, t), {})
                if r.get('status') == 'complete':
                    done[arm, t] = r
                elif r:
                    blocked.append({'arm': arm, 'chain': c, 'task': t, 'reason': r.get('reason')})
                    break
                else:
                    candidates.append({'kind': 'target', 'arm': arm, 'chain': c, 'task': t,
                                       'ordinal': sum(len(m['target_tasks'][x]) for x in m['target_order'][:m['target_order'].index(c)]) + index})
                    break
    # Same fixed task order, least total progress breaks ties. Extra service may
    # work on another chain; never simultaneous dependent tasks within one arm/chain.
    counts = {a: sum(k[0] == a for k in done) for a in ARMS}
    candidates.sort(key=lambda x: (x['ordinal'], counts[x['arm']], ARMS.index(x['arm'])))
    return done, blocked, candidates


def report(root, m=None):
    m = m or read_json(root / 'manifest.json')
    done, blocked, _ = target_scan(root, m)
    order = [t for c in m['target_order'] for t in m['target_tasks'][c]]
    matched = [t for t in order if all((a, t) in done for a in ARMS)]
    full = [c for c in m['target_order'] if all(t in matched for t in m['target_tasks'][c])]
    rows = {}
    for a in ARMS:
        all_rows = [v for k, v in done.items() if k[0] == a]
        paired = [done[a, t]['reward'] for t in matched]
        rows[a] = {'complete_tasks': len(all_rows), 'complete_chains': [c for c in m['target_order'] if all((a, t) in done for t in m['target_tasks'][c])],
                   'unpaired_mean_reward': sum(v['reward'] for v in all_rows) / len(all_rows) if all_rows else None,
                   'matched_tasks': len(matched), 'matched_mean_reward': sum(paired) / len(paired) if paired else None,
                   'matched_full_chain_mean_reward': sum(done[a, t]['reward'] for c in full for t in m['target_tasks'][c]) / sum(len(m['target_tasks'][c]) for c in full) if full else None,
                   'solve_seconds_total': sum(v.get('solve_seconds', 0) for v in all_rows)}
    return {'time': time.time(), 'source': {a: source_progress(m, a) for a in memory.ARMS},
            'banks': {a: read_json(root / 'banks' / a / 'result.json') for a in memory.ARMS},
            'target': rows, 'matched_task_ids': matched, 'matched_full_chains': full, 'blocked': blocked,
            'target_total_solves': len(order) * len(ARMS), 'interpretation': m['interpretation']}


def candidates(root, m):
    source = source_progress(m, 'meta-skill-rsi')
    if source['next'] or source['blocked']:
        return [{'kind': 'source', 'arm': 'meta-skill-rsi', **x} for x in source['next']]
    if not banks_frozen(root):
        return [{'kind': 'merge', 'arm': a} for a in memory.ARMS if not (root / 'banks' / a / 'frozen.json').exists()
                and not (root / 'banks' / a / 'blocked.json').exists()]
    return target_scan(root, m)[2]


def worker_output(root, worker):
    if worker['kind'] == 'source':
        return record_path(SOURCE_ROOT, worker['chain'], 'meta-skill-rsi', worker['task'])
    if worker['kind'] == 'merge':
        return root / 'banks' / worker['arm'] / 'frozen.json'
    return record_path(root, worker['chain'], worker['arm'], worker['task'])


def worker_blocked(root, worker):
    path = worker_output(root, worker)
    return path.with_name('blocked.json') if worker['kind'] == 'merge' else path


def run(root):
    m = verify(root)
    # Occupy the historical dispatcher lock throughout the handoff campaign.
    with legacy.exclusive(SOURCE_ROOT / 'scheduler.lock'), legacy.exclusive(root / 'scheduler.lock'):
        if not (SOURCE_ROOT / 'PAUSE').exists():
            raise RuntimeError('historical 21-chain queue must remain paused')
        active = recovery.adopt_untracked(root, m, read_json(root / 'active.json', {}))
        atomic_json(root / 'active.json', active)
        children, stopping = {}, False
        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            verify(root)
            for port, w in list(active.items()):
                if port in children:
                    children[port].poll()
                if legacy.alive(w):
                    continue
                if recovery.orphaned_child(worker_output(root, w).parent):
                    # The wrapper may die while Harbor is still using the port.
                    # Reserve it; never start another solve behind an orphan.
                    w['orphaned_child'] = True
                    continue
                if not worker_output(root, w).exists():
                    atomic_json(worker_blocked(root, w), {'status': 'blocked', 'reason': 'worker_interrupted; inspect saved solve before recovery', **w})
                del active[port]
            recovered = recovery.recover_available(root, m, active)
            disk = core.docker_disk_status(PROJECT)
            waiting = []
            occupied = {str(p) for p, w in read_json(legacy.CONTROL / 'active.json', {}).items() if legacy.alive(w)}
            occupied.update(str(w[0]) for w in core.live_chain_workers(core.OLD_ROOT))
            endpoints = {str(p): {'model_ready': core.endpoint_ok(p), 'idle': legacy.endpoint_idle(p)} for p in m['ports']}
            queue = candidates(root, m)
            if not stopping and not (root / 'PAUSE').exists() and disk['ok']:
                for port in map(str, m['ports']):
                    if port in active or port in occupied:
                        continue
                    if not endpoints[port]['model_ready'] or not endpoints[port]['idle']:
                        waiting.append({'port': port, 'reason': 'endpoint_unavailable_or_occupied', **endpoints[port]})
                        continue
                    for job in queue:
                        if any((w['kind'], w['arm'], w.get('chain')) == (job['kind'], job['arm'], job.get('chain')) for w in active.values()):
                            continue
                        if job['kind'] == 'source':
                            ok, why = core.ready(job['task'], SOURCE_ROOT)
                        elif job['kind'] == 'target':
                            series = m['target_tasks'][job['chain']]
                            checks = series if job['task'] == series[0] else [job['task']]
                            failed = next(((t, why) for t in checks for ok, why in [target_ready(root, t, m)] if not ok), None)
                            ok, why = not failed, str(failed) if failed else None
                        else:
                            ok, why = True, None
                        if not ok:
                            waiting.append({**job, 'reason': why})
                            continue
                        if job['kind'] == 'source':
                            command = [sys.executable, '-u', '-m', 'cross_chain_rsi.campaign', '--root', str(root),
                                       '--source-stage', '--chain', job['chain'], '--task', job['task'], '--port', port]
                        else:
                            command = [sys.executable, '-u', '-m', 'cross_chain_rsi.campaign', '--root', str(root),
                                       '--' + ('merge' if job['kind'] == 'merge' else 'stage'), '--arm', job['arm'], '--port', port]
                            if job['kind'] == 'target':
                                command += ['--chain', job['chain'], '--task', job['task']]
                        log = root / 'logs' / f"{job['kind']}-{job['arm']}-{job.get('task', 'bank')}.log"
                        log.parent.mkdir(exist_ok=True)
                        with log.open('ab', buffering=0) as stream:
                            child = subprocess.Popen(command, cwd=PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                        children[port] = child
                        active[port] = {**job, 'pid': child.pid, 'identity': core.process_identity(child.pid), 'started': time.time()}
                        atomic_json(root / 'active.json', active)
                        break
            stats = report(root, m)
            completed = sum(v['complete_tasks'] for v in stats['target'].values())
            terminal = 'complete' if completed == stats['target_total_solves'] else None
            if not active and (stopping or (root / 'PAUSE').exists()):
                terminal = 'paused'
            elif not active and not queue and terminal is None:
                terminal = 'needs_review'
            value = {'time': time.time(), 'pid': os.getpid(), 'active': active, 'waiting': waiting, 'disk': disk,
                     'endpoints': endpoints,
                     'occupied_old_ports': sorted(occupied), 'terminal': terminal,
                     'recoveries_this_poll': recovered,
                     'phase': 'target' if banks_frozen(root) else ('source' if (source_progress(m, 'meta-skill-rsi')['next'] or source_progress(m, 'meta-skill-rsi')['blocked']) else 'consolidation')}
            atomic_json(root / 'active.json', active)
            atomic_json(root / 'status.json', value)
            atomic_json(root / 'metrics.json', stats)
            atomic_json(SOURCE_ROOT / 'external_supervisor.json', {'root': str(root), 'status': str(root / 'status.json'), 'time': time.time()})
            snapshot = root / 'monitor' / f'{int(time.time() // 1800)}.json'
            if not snapshot.exists():
                atomic_json(snapshot, {'status': value, 'metrics': stats, 'note': 'persistent diagnostics, NOT a Codex wakeup'})
            if terminal == 'needs_review' or waiting or any(w.get('orphaned_child') for w in active.values()):
                atomic_json(root / 'alerts/latest.json', {'time': time.time(), 'status': value, 'metrics': stats,
                            'message': 'work quarantined; supervisor remains alive; no automatic solver retry'})
            if terminal in ('complete', 'paused'):
                return
            time.sleep(20)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    for flag in ('prepare', 'seal', 'run', 'merge', 'stage', 'source-stage', 'report'):
        g.add_argument('--' + flag, action='store_true')
    p.add_argument('--acceptance', type=Path)
    p.add_argument('--arm', choices=ARMS)
    p.add_argument('--chain')
    p.add_argument('--task')
    p.add_argument('--port', type=int)
    a = p.parse_args()
    root = a.root.resolve()
    if a.prepare:
        prepare(root)
    elif a.seal:
        seal(root, a.acceptance.resolve())
    elif a.report:
        print(json.dumps(report(root), ensure_ascii=False, indent=2))
    elif a.run:
        run(root)
    else:
        try:
            if a.source_stage:
                source_stage(root, a.chain, a.task, a.port)
            elif a.merge:
                merge(root, a.arm, a.port)
            else:
                stage(root, a.chain, a.arm, a.task, a.port)
        except Exception as exc:
            path = (legacy.stage_path(SOURCE_ROOT, a.chain, a.task) / 'record.json') if a.source_stage else ((root / 'banks' / a.arm / 'blocked.json') if a.merge else record_path(root, a.chain, a.arm, a.task))
            if not path.exists():
                atomic_json(path, {'status': 'blocked', 'reason': f'{type(exc).__name__}:{exc}'})
            raise


if __name__ == '__main__':
    main()
