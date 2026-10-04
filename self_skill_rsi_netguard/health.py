"""Distinguish a reachable HTTP error from an unavailable transport."""
import json
from pathlib import Path
import re
import time

from rsi_harness.clean_rsi import read_json
from self_skill_rsi_network import audit, proxy

POLICY = {'version': 'task-evidence-network-admission-v2',
    'probe_interval_seconds': 60, 'preflight_attempts': 3, 'preflight_backoff_seconds': 10,
    'required_healthy_origins': 2, 'sustained_outage_rounds': 3,
    'sustained_outage_min_span_seconds': 90,
    'single_origin_probe_failure': 'warning_not_task_invalidation',
    'http_403_404': 'HTTP_response_not_transport_failure',
    'task_transport_error': 'quarantine_for_review_without_changing_reward',
    'score_based_retries': False}


def classify_round(value):
    checks = value.get('checks', [])
    origins = {c.get('url') for c in checks}
    if len(checks) != 3 or len(origins) != 3:
        raise RuntimeError('incomplete_or_duplicate_network_probe')
    available, transport = 0, 0
    for c in checks:
        status = str(c.get('http_status', '000'))
        code = int(status) if status.isdigit() else 0
        connected = c.get('exit_code') == 0 and 200 <= code <= 599 and code != 407
        transport += connected
        available += connected and 200 <= code < 400
    return {'time': value['time'], 'ready': available >= POLICY['required_healthy_origins'],
            'transport_outage': transport < POLICY['required_healthy_origins'],
            'available_origins': available, 'responding_origins': transport,
            'warning': available != 3}


def sustained_outage(rounds):
    streak = []
    for value in sorted(rounds, key=lambda v: v['time']):
        assessment = classify_round(value)
        if not assessment['transport_outage']:
            streak = []
            continue
        streak.append(value['time'])
        if (len(streak) >= POLICY['sustained_outage_rounds']
                and streak[-1] - streak[0] >= POLICY['sustained_outage_min_span_seconds']):
            return True
    return False


def trace_evidence(path):
    data = read_json(Path(path))
    if not isinstance(data, dict) or not isinstance(data.get('steps'), list):
        raise RuntimeError('missing_trajectory_for_network_admission')
    failures = audit.proxy_failures(path)
    observations, successes = [], []
    pattern = re.compile(
        r'^curl: \((?:5|6|7|18|28|35|52|56|60)\)|'
        r'^fatal:.*(?:Could not resolve|Failed to connect|Couldn.t connect|timed out|SSL)|'
        r'^[WE]: Failed to fetch .*?(?:connect|resolv|timed out|TLS|SSL)|'
        r'^\s*(?:URLError|OSError|ConnectionError).*?(?:unreachable|refused|timed out)', re.I)
    for step in data['steps']:
        observation = step.get('observation') or {}
        for item in observation.get('results', []) if isinstance(observation, dict) else []:
            content = item.get('content', '')
            if not isinstance(content, str): continue
            observations.append(content)
            for line in content.splitlines():
                if pattern.search(line):
                    failures.append({'time': step.get('timestamp'), 'line': line[:500]})
                if re.search(r'^Fetched \S+ \S+ in |^HTTP/(?:1\.1|2|3) [23]\d\d|^Setting up (?:coq|ocaml|curl|git)', line):
                    successes.append({'time': step.get('timestamp'), 'line': line[:300]})
    # A missing executable, an invented repository/404, and a shell parser warning
    # are not network failures. Do not inspect reward when classifying transport.
    return {'transport_failures': failures, 'positive_network_evidence': successes,
            'observations_present': bool(observations)}


def decide(trajectory, rounds):
    if not rounds: raise RuntimeError('network_monitor_evidence_missing')
    evidence = trace_evidence(trajectory)
    sustained = sustained_outage(rounds)
    accepted = not evidence['transport_failures'] and not sustained and evidence['observations_present']
    return {'accepted': accepted, 'reason': 'task_network_error' if evidence['transport_failures']
        else 'sustained_multi_origin_outage' if sustained else 'no_observed_task_network_impairment',
        'sustained_outage': sustained, 'probe_rounds': len(rounds),
        'warning_rounds': sum(classify_round(v)['warning'] for v in rounds),
        **evidence, 'policy': POLICY, 'reward_used_for_decision': False}


def sample():
    value = proxy.health()
    return {**value, 'assessment': classify_round(value)}
