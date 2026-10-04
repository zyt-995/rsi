import argparse
import copy
from pathlib import Path
import time

from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from self_skill_rsi_expand import campaign as previous
from terminal_offline_expand.assets import validate

base, v2 = previous.base, previous.v2
ARMS, PORTS, GROUPS = previous.ARMS, previous.PORTS, previous.GROUPS
control_record, official, finished = previous.control_record, previous.official, previous.finished
next_entry, report, acceptance = previous.next_entry, previous.report, previous.acceptance


def code():
    return {str(p.resolve()): file_hash(p) for p in sorted(Path(__file__).parent.glob('*.py'))}


def prepare(root):
    previous.verify(root)
    if any(base.legacy.alive(read_json(p)) for p in (root / 'jobs').glob('*/*/launch.json')):
        raise RuntimeError('cannot_transition_with_active_workers')
    target = root / 'recovery/protocol.json'
    if target.exists(): return verify(root)
    frozen = {}
    for p in list((root / 'stream').glob('*/scored.json')) + list((root / 'stream').glob('*/record.json')) + list((root / 'controls').glob('*/*/stages/*/record.json')):
        frozen[str(p)] = file_hash(p)
    bank = read_json(root / 'bank/HEAD.json')
    atomic_json(target, {'time': time.time(), 'code': code(), 'preserved_results': frozen,
        'bank_at_transition': bank, 'algorithm_changed': False, 'task_order_changed': False,
        'authorisation': 'user requested autonomous infrastructure repair and continued evaluation',
        'asset_policy': 'only exact SHA256 LFS blobs; journal verifies all other files unchanged',
        'retry_policy': 'only infrastructure preparation retries automatically; never sample an already scored solve'})
    return verify(root)


def verify(root):
    m = previous.verify(root)
    recovery = read_json(root / 'recovery/protocol.json')
    if not recovery or recovery['code'] != code(): raise RuntimeError('recovery_code_changed')
    base.verify_files(recovery['preserved_results'])
    return m


def resolved_manifest(root, m):
    result = copy.deepcopy(m)
    registry = {}
    for chain in read_json(root / 'queue.json')['chain_order']:
        value = acceptance(root, chain)
        if value: registry.update({e['task']: e for e in value['entries']})
    for i, entry in enumerate(result['stream'][112:], 112):
        resolved = registry.get(entry['task'])
        if not resolved: continue
        if any(resolved[k] != entry[k] for k in ('uid', 'task', 'chain', 'version', 'path')):
            raise RuntimeError('recovery_registered_wrong_task')
        receipt_path = root / 'asset-receipts' / (entry['task'] + '.json')
        receipt = read_json(receipt_path)
        expected = entry
        if receipt:
            journal_path = Path(receipt['journal'])
            if file_hash(journal_path) != receipt['journal_sha256']: raise RuntimeError('asset_journal_changed')
            expected = validate(entry, read_json(journal_path))
        if any(resolved[k] != expected[k] for k in ('task_checksum', 'environment_fingerprint')):
            raise RuntimeError('unregistered_asset_change')
        result['stream'][i] = resolved
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--root', required=True, type=Path); p.add_argument('--prepare', action='store_true')
    a = p.parse_args(); prepare(a.root) if a.prepare else verify(a.root)
