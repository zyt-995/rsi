import argparse
from pathlib import Path
import re
import time

from harbor.models.task.task import Task
from rsi_harness.clean_campaign import DATASET, OLD_ROOT, tasks
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash, fingerprint

SOURCE = Path('/data2/ytzhang/rsi-harness-runs/self-skill-controls-20261003-data2-v1')
CONTROL = Path('/home/ytzhang/rsi-harness/runs/clean-rsi-20260923-qwen38-27b')
ARMS = ('baseline', 'skill-rsi', 'self-skill')


def make(root):
    output = root / 'queue.json'
    if output.exists(): return read_json(output)
    previous = read_json(SOURCE / 'manifest.json')
    seen = {e['task'] for e in previous['stream']}
    old_chains = list(dict.fromkeys(e['chain'] for e in previous['stream']))
    all_names = {p.parent.name for p in DATASET.glob('*/task.toml')}
    groups = sorted({re.split(r'-EVO-|-v-', name)[0] for name in all_names})
    chain_order = [c for c in old_chains if any(t not in seen for t in tasks(c))]
    chain_order += [c for c in groups if c not in old_chains]
    remaining = []
    for chain in chain_order:
        for version, task in enumerate(tasks(chain)):
            if task in seen: continue
            path = DATASET / task
            remaining.append({'uid': f's{112+len(remaining):06d}', 'chain': chain, 'task': task,
                'version': version, 'path': str(path), 'task_checksum': Task(path).checksum,
                'environment_fingerprint': fingerprint(path),
                'old_ticket': str(OLD_ROOT / '_infra/readiness' / f'{task}.json')})
    if len(seen) != 112 or len(remaining) != 329 or seen | {e['task'] for e in remaining} != all_names:
        raise RuntimeError('unexpected_dataset_inventory')
    value = {'source': str(SOURCE), 'source_manifest_sha256': file_hash(SOURCE / 'manifest.json'),
        'created': time.time(), 'arms': ARMS, 'prior': previous['stream'], 'remaining': remaining,
        'chain_order': chain_order, 'selection': 'complete_old_chain_tails_then_sorted_new_chains;not_reward_based',
        'total_tasks': 441, 'first_report_new_chains': 10,
        'interpretation': 'historical continuation, unequal update compute and memory scope; not an isolated causal comparison'}
    atomic_json(output, value)
    return value


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path)
    q = make(p.parse_args().root.resolve())
    print({'total_tasks': q['total_tasks'], 'remaining': len(q['remaining']), 'first': q['remaining'][0]['task']})
