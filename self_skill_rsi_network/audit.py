"""Only terminal observations, not model explanations, establish proxy outages."""
import re
from pathlib import Path
from rsi_harness.clean_rsi import read_json


def proxy_failures(trajectory):
    evidence = []
    for step in read_json(Path(trajectory), {}).get('steps', []):
        observation = step.get('observation') or {}
        for result in observation.get('results', []) if isinstance(observation, dict) else []:
            content = result.get('content', '')
            if not isinstance(content, str):
                continue
            for line in content.splitlines():
                if (re.search(r'(?:10\.0\.2\.2|127\.0\.0\.1)(?::| port )1789[78]', line)
                        and re.search(r'refused|Could not connect|Unable to connect|Failed to connect', line, re.I)):
                    evidence.append({'time': step.get('timestamp'), 'line': line[:400]})
    return evidence


def record_failures(record):
    return proxy_failures(Path(record['result']).parent / 'agent/trajectory.json')


def inventory(root):
    rows = []
    for scored in sorted(root.glob('stream/*/scored.json')):
        value = read_json(scored)
        found = record_failures(value)
        if found:
            rows.append({'kind': 'formal', 'uid': value['uid'], 'task': value['task'],
                         'record': str(scored), 'result': value['result'], 'evidence': found})
    for record in sorted(root.glob('stream/*/validation/*/00/candidate/record.json')):
        value = read_json(record)
        found = record_failures(value)
        if found:
            rows.append({'kind': 'validation', 'stage_uid': record.relative_to(root).parts[1],
                         'uid': value['uid'], 'task': value['task'], 'record': str(record),
                         'result': value['result'], 'evidence': found})
    return rows
