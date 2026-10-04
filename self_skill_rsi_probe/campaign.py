"""Three matched old/new skill replications, fixed budget, no adaptive selection."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import signal
import threading
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi import bank
from self_skill_rsi_resilient import campaign as source_campaign, grading, runtime
from self_skill_rsi_accel.environment import runner_environment

base = source_campaign.base
PROJECT = base.core.PROJECT
POLICY = dict(source_campaign.POLICY)
MODULE = 'self_skill_rsi_probe.campaign'


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def prepare(root, source, deployment):
    root, source = root.resolve(), source.resolve()
    with base.legacy.exclusive(root / 'prepare.lock'):
        if (root / 'manifest.json').exists():
            return verify(root)
        if any(p.name != 'prepare.lock' for p in root.iterdir()):
            raise RuntimeError('probe_target_not_empty')
        old = source_campaign.verify(source)
        if read_json(source / 'status.json').get('state') != 'complete':
            raise RuntimeError('source_not_complete')
        deployment = deployment.resolve()
        deployed = read_json(deployment)
        if deployed.get('ports') != [8040, 8041] or not deployed.get('functional_checks_passed'):
            raise RuntimeError('inference_smoke_missing')
        frozen = {str(p): file_hash(p) for p in (source / 'manifest.json', source / 'seal.json', deployment)}
        frozen.update(deployed['evidence_hashes'])
        stores = {}
        stage = source / 'stream/s000012'
        for label, prior in [('old', 'parent'), ('new', 'candidate')]:
            src = stage / prior
            head = read_json(src / 'HEAD.json')
            payload, provenance = bank.load(src, head)
            store = root / 'stores' / label
            copied = bank.publish(store, payload, provenance, head['revision'], head['after_task'])
            if copied != head:
                raise RuntimeError('skill_copy_changed')
            stores[label] = {'path': str(store), 'head': head, 'source': str(src)}
            for p in list(src.rglob('*')) + list(store.rglob('*')):
                if p.is_file():
                    frozen[str(p)] = file_hash(p)
        # Seal all original official score/decision records, without copying or
        # presenting those scores as additional independent replicates.
        for p in sorted(source.glob('stream/*/*.json')):
            if p.name in ('record.json', 'scored.json'):
                frozen[str(p)] = file_hash(p)
        entry = copy.deepcopy(next(e for e in old['stream'] if e['uid'] == 's000012'))
        jobs = []
        for repeat in range(3):
            for offset, arm in enumerate(('old', 'new')):
                jobs.append({**entry, 'uid': f'p{repeat:02d}-{arm}', 'source_uid': entry['uid'],
                             'arm': arm, 'repeat': repeat,
                             'port': [8040, 8041][(repeat + offset) % 2]})
        change = old['resilience_migration']
        m = {'schema': 'self-skill-held-in-probe-v1', 'created': time.time(),
             'source_root': str(source), 'model': old['model'], 'solve': old['solve'],
             'ports': [8040, 8041], 'stream': jobs, 'stores': stores,
             'grader': change['grader'], 'grader_sha256': change['grader_sha256'],
             'inference_migration': {'deployment': str(deployment)},
             'implementation': code(), 'frozen_files': frozen,
             'common_change': 'remaining-budget disclosure and explicit termination accounting only',
             'retrieval': 'unchanged four documents / 12000 chars; no relevance intervention',
             'sampling': 'unchanged model/server defaults, unseeded repeats; endpoint assignment alternates',
             'claims': 'selected seen-task replication, not unseen transfer or a preregistered efficacy study; '
                       'six fresh solves separate from all historical scores',
             'gate': 'run all three pairs regardless of win/loss; never retry a valid zero',
             'policy': POLICY}
        atomic_json(root / 'manifest.json', m)
        atomic_json(root / 'seal.json', {'manifest_sha256': file_hash(root / 'manifest.json')})
        atomic_json(root / 'status.json', {'state': 'prepared_not_running', 'time': time.time()})
        return verify(root)


def verify(root):
    m = read_json(root / 'manifest.json')
    if (not m or read_json(root / 'seal.json')['manifest_sha256'] != file_hash(root / 'manifest.json')
            or m['implementation'] != code() or m['policy'] != POLICY):
        raise RuntimeError('probe_seal_changed')
    source_campaign.verify(Path(m['source_root']))
    base.verify_files(m['frozen_files'])
    grading.load_manifest(m['grader'], m['grader_sha256'])
    for s in m['stores'].values():
        bank.load(Path(s['path']), s['head'])
    return m


@contextmanager
def adapter(m):
    original = base.core.harbor_command
    original_env = base.core.runner_environment
    def command(*args, **kwargs):
        value = source_campaign.prior.offline.ORIGINAL_COMMAND(*args, **kwargs)
        value[value.index('--agent-import-path') + 1] = 'self_skill_rsi_probe.agent:BudgetAwareAgent'
        return value + ['--verifier', 'self_skill_rsi_resilient.harbor:ResilientVerifier',
            '--verifier-kwarg', 'manifest=' + m['grader'],
            '--verifier-kwarg', 'manifest_sha256=' + m['grader_sha256'],
            '--environment-kwarg', 'keep_containers=true']
    base.core.harbor_command = command
    # Docker environment is set before entering this context. The original
    # environment helper safely inherits it; avoid recursive monkey-patching.
    try:
        yield
    finally:
        base.core.harbor_command = original
        base.core.runner_environment = original_env


def report(root, m):
    rows = []
    for e in m['stream']:
        p = root / 'stream' / e['uid'] / 'solve/record.json'
        r = read_json(p)
        if r:
            base.verify_files(r['hashes'])
            rows.append({'uid': e['uid'], 'arm': e['arm'], 'repeat': e['repeat'],
                         'reward': r['reward'], 'seconds': r['solve_seconds'],
                         'stop_reason': r['runtime']['stop_reason'], 'result': r['result']})
    arms = {arm: {'completed': sum(r['arm'] == arm for r in rows),
                  'passed': sum(r['reward'] for r in rows if r['arm'] == arm)} for arm in ('old', 'new')}
    pairs = []
    for i in range(3):
        matched = {r['arm']: r for r in rows if r['repeat'] == i}
        if set(matched) == {'old', 'new'}:
            pairs.append({'repeat': i, 'old': matched['old']['reward'], 'new': matched['new']['reward'],
                          'delta': matched['new']['reward'] - matched['old']['reward']})
    return {'time': time.time(), 'cohort': 'held_in_replication', 'scored_tasks': len(rows),
            'total_tasks': len(m['stream']), 'unique_tasks': 1, 'arms': arms, 'paired': pairs,
            'rows': rows, 'historical_scores_reused_as_replicates': 0, 'effectiveness_proven': False}


def run(root):
    with base.legacy.exclusive(root / 'scheduler.lock'):
        m = verify(root)
        os.environ.update(runner_environment(m['ports'][0]))
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        heart = threading.Thread(target=base.heartbeat, args=(root, stop), daemon=True)
        heart.start()
        try:
            for p in root.glob('stream/**/child.json'):
                runtime.wait_child(p.parent)
            with source_campaign.prior.offline.runtime.inference_lease(m['ports']), adapter(m):
                grader = grading.load_manifest(m['grader'], m['grader_sha256'])
                def solve(e):
                    verify(root)
                    store = m['stores'][e['arm']]
                    result = runtime.native_solve(e, Path(store['path']), store['head'],
                        root / 'stream' / e['uid'] / 'solve', e['port'], 'probe', manifest=grader)
                    if not result['runtime'].get('stop_reason'):
                        raise RuntimeError('termination_accounting_missing')
                    return result
                for repeat in range(3):
                    pending = [e for e in m['stream'] if e['repeat'] == repeat
                               and not (root / 'stream' / e['uid'] / 'solve/record.json').exists()]
                    if not pending:
                        continue
                    if stop.is_set() or (root / 'PAUSE').exists():
                        atomic_json(root / 'status.json', {'state': 'paused', 'time': time.time()})
                        return
                    atomic_json(root / 'status.json', {'state': 'running', 'time': time.time(),
                        'pid': os.getpid(), 'identity': base.core.process_identity(os.getpid()),
                        'task': pending[0]['task'], 'repeat': repeat, 'active': [e['uid'] for e in pending]})
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        list(pool.map(solve, pending))
                    atomic_json(root / 'metrics.json', report(root, m))
                atomic_json(root / 'status.json', {'state': 'complete', 'time': time.time(),
                            'completed': len(m['stream']), 'next_action': 'review_three_pairs_before_expansion'})
        except Exception as exc:
            atomic_json(root / 'status.json', {'state': 'needs_attention', 'time': time.time(),
                'reason': type(exc).__name__ + ':' + str(exc), 'invalid_is_not_zero': True})
        finally:
            stop.set()
            heart.join(timeout=5)
            atomic_json(root / 'metrics.json', report(root, m))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--prepare-from', type=Path)
    p.add_argument('--deployment', type=Path)
    p.add_argument('--run', action='store_true')
    p.add_argument('--allow-native-runs', action='store_true')
    p.add_argument('--report', action='store_true')
    a = p.parse_args()
    if a.prepare_from:
        prepare(a.root, a.prepare_from, a.deployment)
    elif a.run and a.allow_native_runs:
        run(a.root)
    elif a.report:
        print(json.dumps(report(a.root, verify(a.root)), indent=2))
    else:
        p.error('explicit action required')
