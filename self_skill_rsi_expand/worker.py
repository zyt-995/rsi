"""One leased arm step. No successful formal solve or interrupted request is retried."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from rsi_harness import clean_campaign as clean
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi import runtime as native_runtime
from self_skill_rsi_resilient import runtime as resilient, grading
from self_skill_rsi_netguard import runtime as network, health
from self_skill_rsi_accel.environment import runner_environment
from self_skill_rsi_storage import storage
from . import campaign as c


def grader(root, m, task):
    entry = next(e for e in m['stream'] if e['task'] == task)
    if int(entry['uid'][1:]) < 112:
        path = Path(m['resilience_migration']['grader']); expected = m['resilience_migration']['grader_sha256']
    else:
        value = c.acceptance(root, entry['chain'])
        if value is None: raise RuntimeError('chain_not_accepted')
        path, expected = Path(value['grader']), value['grader_sha256']
    return path, expected, grading.load_manifest(path, expected)


@contextmanager
def adapter(root, m):
    old_command, old_disk = clean.harbor_command, clean.docker_disk_status
    def command(task_path, *a, **kw):
        path, expected, _ = grader(root, m, Path(task_path).name)
        values = old_command(task_path, *a, **kw)
        values = [v.replace('=http://10.0.2.2:17897', '=http://10.0.2.2:17898') for v in values]
        return values + ['--verifier', 'self_skill_rsi_resilient.harbor:ResilientVerifier',
            '--verifier-kwarg', 'manifest=' + str(path), '--verifier-kwarg', 'manifest_sha256=' + expected,
            '--environment-kwarg', 'keep_containers=true']
    clean.harbor_command = command
    clean.docker_disk_status = lambda project: storage.disk_status(root)
    try: yield
    finally: clean.harbor_command, clean.docker_disk_status = old_command, old_disk


def control_step(root, m, entry, arm, port):
    folder = c.control_record(root, entry, arm).parent
    if c.official(root, entry, arm, m): return
    disk = storage.disk_status(root)
    if not disk['ok']: raise RuntimeError('disk_admission_failed:' + str(disk))
    probe = health.sample(); network.save_probe(folder, 'preflight', probe)
    if not probe['assessment']['ready']: raise RuntimeError('network_preflight_unavailable_no_model_call')
    stop, errors = threading.Event(), []
    def monitor():
        while not stop.wait(health.POLICY['probe_interval_seconds']):
            try: network.save_probe(folder, 'during', health.sample())
            except Exception as exc:
                errors.append(str(exc)); atomic_json(folder / 'network-monitor-error.json', {'errors': errors})
    thread = threading.Thread(target=monitor, daemon=True); thread.start()
    original_check, original_reuse = clean.scored_result, clean.legacy_baseline
    def checked(path):
        valid, reason = original_check(path)
        if not valid: return valid, reason
        network.save_probe(folder, 'postflight', health.sample())
        decision = health.decide(path.parent / 'agent/trajectory.json', network.all_probes(folder))
        if errors: decision.update(accepted=False, reason='network_monitor_failed')
        atomic_json(folder / ('network-admission.json' if decision['accepted'] else 'network-rejected-v2.json'),
            {**decision, 'result': str(path), 'result_sha256': file_hash(path), 'reward_used_for_decision': False})
        return decision['accepted'], None if decision['accepted'] else decision['reason']
    clean.scored_result, clean.legacy_baseline = checked, lambda *a: None
    try:
        result = clean.run_stage(SimpleNamespace(root=root / 'controls', chain=entry['chain'], arm=arm,
            task=entry['task'], port=port, old_root=Path(entry['readiness_root'])))
        if result or not c.official(root, entry, arm, m): raise RuntimeError('control_step_not_admitted')
        record = read_json(c.control_record(root, entry, arm)); resilient.clean_success(record)
    finally:
        clean.scored_result, clean.legacy_baseline = original_check, original_reuse
        stop.set(); thread.join(timeout=20)


def run(root, arm, uid, port, output):
    storage.activate(); os.environ.update(runner_environment(port))
    m = c.resolved_manifest(root, c.verify(root)); entry = next(e for e in m['stream'] if e['uid'] == uid)
    expected = c.next_entry(root, m, arm)
    if expected and expected['uid'] != uid: raise RuntimeError('arm_order_violation')
    c.base.core.ready(entry['task'], Path(entry['readiness_root']))
    try:
        with native_runtime.inference_lease([port]), adapter(root, m):
            if arm == 'self-skill':
                m['ports'] = [port]
                def solve(e, *a, **kw):
                    _, _, safe = grader(root, m, e['task'])
                    return network.native(root, e, *a, **kw, manifest=safe)
                c.v2.step(root, m, int(uid[1:]), solve=solve, proposer=resilient.propose)
            else:
                control_step(root, m, entry, arm, port)
        atomic_json(output, {'state': 'complete', 'arm': arm, 'uid': uid, 'task': entry['task'], 'time': time.time()})
    except Exception as exc:
        atomic_json(output, {'state': 'needs_attention', 'arm': arm, 'uid': uid, 'task': entry['task'],
            'time': time.time(), 'reason': type(exc).__name__ + ':' + str(exc)[-3000:], 'automatic_model_replay': False})
        raise


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', type=Path, required=True)
    p.add_argument('--arm', choices=c.ARMS, required=True); p.add_argument('--uid', required=True)
    p.add_argument('--port', type=int, required=True); p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); run(a.root.resolve(), a.arm, a.uid, a.port, a.output)
