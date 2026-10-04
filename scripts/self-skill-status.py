#!/usr/bin/env python3
"""Read-only active pipeline status; never restarts or repeats a trial."""
import argparse
import importlib
import json
from pathlib import Path
import sys
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from rsi_harness.clean_rsi import read_json


def get_status():
    active = read_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json')
    module = active['campaign_module']
    if module not in ('self_skill_rsi_offline.campaign', 'self_skill_rsi_accel.campaign',
                      'self_skill_rsi_resilient.campaign', 'self_skill_rsi_probe.campaign',
                      'self_skill_rsi_continue.campaign', 'self_skill_rsi_dual.campaign',
                      'self_skill_rsi_resume.campaign', 'self_skill_rsi_network.campaign',
                      'self_skill_rsi_netguard.campaign', 'self_skill_rsi_storage.campaign',
                      'self_skill_rsi_expand.campaign', 'self_skill_rsi_recover.campaign'):
        raise RuntimeError('unsupported_active_campaign_module')
    c = importlib.import_module(module)
    root = Path(active['root'])
    m = c.verify(root)
    status = read_json(root / 'status.json', {})
    metrics = c.report(root, m) if hasattr(c, 'report') else c.v2.report(root, m)
    value = {'time': time.time(), 'root': str(root), 'module': module, 'seals_valid': True,
             'status': status, 'controller_alive': c.base.legacy.alive(read_json(root / 'supervisor_launch.json', {})),
             'watchdog': read_json(root / 'watchdog/status.json', {}), 'metrics': metrics,
             'handoff': read_json(Path('/data2/ytzhang/rsi-harness-runs/inference-acceleration-20260930/handoff-status.json'), {}),
             'resource_handoff': read_json(Path(active['resource_handoff_status']), {})
                 if active.get('resource_handoff_status') else None,
             'endpoints': {}, 'live_trials': []}
    for port in m['ports']:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=3) as r:
                lines = r.read().decode().splitlines()
            value['endpoints'][port] = {'reachable': True, 'requests': [x for x in lines if x.startswith(
                ('vllm:num_requests_running{', 'vllm:num_requests_waiting{'))]}
        except OSError as exc:
            value['endpoints'][port] = {'reachable': False, 'error': type(exc).__name__}
    for p in list(root.glob('stream/**/child.json')) + list(root.glob('controls/**/child.json')):
        if c.base.legacy.alive(read_json(p)):
            value['live_trials'].append(str(p.relative_to(root)))
    # During a boundary handoff, retired spare endpoints do not indicate a
    # failed native trial. The phase and live trials are deliberately explicit.
    return value


def compact_status(value):
    """Keep runtime liveness separate from the last saved scientific stage."""
    metrics, status = value['metrics'], value['status']
    guardian = value.get('watchdog', {})
    root = Path(value['root'])
    controller = read_json(root / 'supervisor_launch.json', {})
    if value['module'] == 'self_skill_rsi_probe.campaign':
        return {'time': value['time'], 'root': value['root'], 'seals_valid': value['seals_valid'],
            'state': status.get('state'), 'task': status.get('task'), 'repeat': status.get('repeat'),
            'controller_alive': value['controller_alive'], 'metrics': metrics,
            'live_trials': value['live_trials'], 'endpoints': value['endpoints'],
            'guardian_last_check_age_seconds': value['time'] - guardian.get('time', 0),
            'guardian_alert': guardian.get('alert'), 'reason': status.get('reason'),
            'automatic_model_replay': False}
    recovering = bool(value['controller_alive'] and value['live_trials']
                      and status.get('pid') != controller.get('pid'))
    return {'time': value['time'], 'root': value['root'], 'seals_valid': value['seals_valid'],
        'state': 'recovering_existing_trial_without_replay' if recovering else status.get('state'),
        'task': status.get('task'), 'controller_alive': value['controller_alive'],
        'completed_steps': metrics['completed_update_steps'], 'scored_tasks': metrics['scored_tasks'],
        'total_tasks': metrics['total_tasks'], 'mean_reward': metrics['official_mean_reward'],
        'skills': metrics.get('active_skill_ids'), 'chains': metrics['chains'],
        'live_trials': value['live_trials'], 'endpoints': value['endpoints'],
        'guardian_last_check_age_seconds': value['time'] - guardian.get('time', 0),
        'guardian_alert': guardian.get('alert'), 'expected_controller_recovery_wait': recovering,
        'reason': status.get('reason'), 'automatic_model_replay': False,
        'continuation': metrics.get('continuation'),
        'inference': metrics.get('inference'),
        'resource_handoff': value.get('resource_handoff'),
        'network_recovery': metrics.get('network_recovery'),
        'network_admission': metrics.get('network_admission'),
        'storage': metrics.get('storage'),
        'arms': metrics.get('arms'),
        'extension_paired': metrics.get('extension_paired'),
        'environment_progress': metrics.get('environment_progress'),
        'environment_ready_chains': metrics.get('environment_ready_chains'),
        'environment_errors': metrics.get('environment_errors'),
        'historical_matched_comparison': metrics.get('historical_matched_comparison')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    value = get_status()
    print(json.dumps(compact_status(value) if args.compact else value, ensure_ascii=False, indent=2))
