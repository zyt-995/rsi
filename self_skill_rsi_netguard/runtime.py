"""Keep network evidence across crashes and reuse only audited native records."""
from pathlib import Path
import threading
import time
import uuid

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_network import campaign as old
from . import health

SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261002-network-v1')


def save_probe(folder, kind, value):
    path = folder / 'network-v2' / f'{time.time_ns()}-{kind}-{uuid.uuid4().hex[:8]}.json'
    atomic_json(path, value)
    return path


def all_probes(folder):
    # Never overwrite previous probes or discard them after a controller restart.
    return [read_json(p) for p in sorted((folder / 'network-v2').glob('*.json'))]


def admit(folder, record):
    path = Path(record['result']).parent / 'agent/trajectory.json'
    decision = health.decide(path, all_probes(folder))
    decision.update(result=record['result'], time=time.time(), automatic_model_replay=False)
    if not decision['accepted']:
        atomic_json(folder / 'network-rejected-v2.json', decision)
        raise RuntimeError('task_network_incident_requires_review_no_automatic_replay')
    evidence = {str(p): file_hash(p) for p in (folder / 'network-v2').glob('*.json')}
    evidence[str(path)] = file_hash(path)
    decision['evidence_hashes'] = evidence
    atomic_json(folder / 'network-admission.json', decision)
    atomic_json(folder / 'record.json', record)
    return record


def native(root, entry, store, head, folder, port, purpose, *, manifest):
    folder = Path(folder)
    if (folder / 'network-rejected-v2.json').exists():
        raise RuntimeError('task_network_incident_quarantined')
    if (folder / 'network-monitor-error.json').exists():
        raise RuntimeError('persisted_network_monitor_error_requires_review')
    decision = read_json(folder / 'network-admission.json')
    cached = read_json(folder / 'record.json')
    if cached:
        if not decision or not decision['accepted']:
            raise RuntimeError('native_record_missing_network_admission')
        old.base.verify_files(decision['evidence_hashes'])
        old.v2.gate.validate_record(entry, cached, purpose, head)
        return cached
    # Recover a crash after a completed solve without doing that solve again.
    raw = read_json(folder / 'execution/record.json')
    if raw:
        old.v2.gate.validate_record(entry, raw, purpose, head)
        if not list((folder / 'network-v2').glob('*-postflight-*.json')):
            save_probe(folder, 'postflight', health.sample())
        return admit(folder, raw)
    # Prior runs can only be reused with identical immutable native inputs.
    # Original SSH-outage examples will fail the observed-transport check.
    for source in (SOURCE, old.SOURCE):
        previous = source / folder.relative_to(root)
        saved = read_json(previous / 'record.json')
        if not old.can_reuse(saved, entry, head, purpose): continue
        trace = Path(saved['result']).parent / 'agent/trajectory.json'
        if health.trace_evidence(trace)['transport_failures']: continue
        old.v2.audit_native(entry, saved, purpose, head)
        hashes = {str(previous / 'record.json'): file_hash(previous / 'record.json'), str(trace): file_hash(trace)}
        atomic_json(folder / 'network-admission.json', {'accepted': True, 'historical_reuse': True,
            'new_model_calls': 0, 'source_record': str(previous / 'record.json'),
            'reason': 'identical native inputs, original record admitted, no observed transport error',
            'evidence_hashes': hashes, 'reward_used_for_decision': False})
        atomic_json(folder / 'record.json', saved)
        return saved
    ready = False
    for attempt in range(health.POLICY['preflight_attempts']):
        probe = health.sample(); save_probe(folder, 'preflight', probe)
        if probe['assessment']['ready']:
            ready = True; break
        if attempt + 1 < health.POLICY['preflight_attempts']:
            time.sleep(health.POLICY['preflight_backoff_seconds'])
    if not ready: raise RuntimeError('sustained_preflight_unavailable_no_model_call')
    stop = threading.Event()
    errors = []
    def monitor():
        while not stop.wait(health.POLICY['probe_interval_seconds']):
            try: save_probe(folder, 'during', health.sample())
            except Exception as exc:
                errors.append(type(exc).__name__ + ':' + str(exc))
                atomic_json(folder / 'network-monitor-error.json', {'errors': errors, 'time': time.time()})
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        record = old.ORIGINAL_NATIVE(entry, store, head, folder / 'execution', port, purpose, manifest=manifest)
    finally:
        stop.set(); thread.join(timeout=20)
    if thread.is_alive() or errors or (folder / 'network-monitor-error.json').exists():
        if thread.is_alive():
            atomic_json(folder / 'network-monitor-error.json', {'time': time.time(), 'error': 'monitor_thread_did_not_finish'})
        raise RuntimeError('network_monitor_incomplete_requires_review')
    save_probe(folder, 'postflight', health.sample())
    return admit(folder, record)
