"""One bounded, audited record recovery from an already completed solve."""
from pathlib import Path
import os
import time

from rsi_harness import meta_skill_campaign as legacy
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash
from . import admission


def orphaned_child(folder):
    child = read_json(folder / 'child.json', {})
    return child if child and legacy.alive(child) else None


def adopt_untracked(root, manifest, active, proc_root=Path('/proc')):
    """Close the dispatcher-crash window between Popen and active.json save."""
    from . import campaign as c
    found = dict(active)
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / 'cmdline').read_bytes().decode().split('\0')[:-1]
            if '-m' not in args or '--root' not in args or '--port' not in args:
                continue
            module = args[args.index('-m') + 1]
            run_root = args[args.index('--root') + 1]
            port = args[args.index('--port') + 1]
            if int(port) not in manifest['ports']:
                continue
            if module == 'cross_chain_rsi.campaign' and run_root == str(root):
                kind = 'source' if '--source-stage' in args else ('target' if '--stage' in args else ('merge' if '--merge' in args else None))
                if not kind:
                    continue
            elif module == 'rsi_harness.meta_skill_campaign' and run_root == str(c.SOURCE_ROOT) and '--stage' in args:
                kind = 'source'
            else:
                continue
            arm = 'meta-skill-rsi' if kind == 'source' else args[args.index('--arm') + 1]
            if arm not in (c.memory.ARMS if kind == 'merge' else c.ARMS):
                continue
            item = {'kind': kind, 'arm': arm}
            if kind != 'merge':
                chain, task = args[args.index('--chain') + 1], args[args.index('--task') + 1]
                order = manifest['source_tasks' if kind == 'source' else 'target_tasks']
                if chain not in order or task not in order[chain]:
                    continue
                item.update(chain=chain, task=task)
            pid = int(proc.name)
            identity = c.core.process_identity(pid)
            if not identity:
                continue
        except (OSError, ValueError, IndexError):
            continue
        prior = found.get(port)
        if prior and prior['pid'] == pid and prior.get('identity') == identity:
            continue
        if prior and (legacy.alive(prior) or orphaned_child(c.worker_output(root, prior).parent)):
            raise RuntimeError('multiple live workers for endpoint:' + port)
        found[port] = {**item, 'pid': pid, 'identity': identity, 'started': time.time(), 'adopted_after_dispatcher_restart': True}
    return found


def recover_record(root, path, task, arm):
    with legacy.exclusive(path.parent / 'recovery.lock'):
        record = read_json(path, {})
        if record.get('status') != 'blocked' or orphaned_child(path.parent):
            return None
        reason = record.get('reason', '')
        if not ('invalid_result:verifier_infrastructure:' in reason or reason.startswith('worker_interrupted')):
            return None
        trials = list((path.parent / 'trials').glob('*/result.json'))
        if len(trials) != 1 or not (path.parent / 'launch.json').exists():
            return None
        result = trials[0]
        verdict = admission.assess(result)
        if not verdict['valid']:
            return None
        # At most one recovery admission per raw solve. A second failure remains
        # quarantined; no hot retry loop and never another model solve.
        archive = root / 'recoveries' / arm / task / file_hash(result)
        if (archive / 'record.before.json').exists():
            return None
        launch = read_json(path.parent / 'launch.json')
        start = read_json(result.parent / 'agent/rsi_start.json', {})
        if not start or start.get('version') != launch.get('before'):
            return None
        value = {'time': time.time(), 'record': str(path), 'record_sha256': file_hash(path),
                 'result': str(result), 'result_sha256': file_hash(result), 'verdict': verdict,
                 'action': 'archive blocked record; resume existing solve result only; NO solver replay'}
        atomic_json(archive / 'approval.json', value)
        # Recoverable move on the same filesystem: original bytes remain intact.
        path.rename(archive / 'record.before.json')
        atomic_json(archive / 'resumed.json', {'time': time.time()})
        return value


def recover_available(root, manifest, active):
    from . import campaign as c
    occupied = {(w.get('kind'), w.get('arm'), w.get('chain')) for w in active.values()}
    recovered = []
    for chain in manifest['source_order']:
        if ('source', 'meta-skill-rsi', chain) in occupied:
            continue
        for task in manifest['source_tasks'][chain]:
            path = c.record_path(c.SOURCE_ROOT, chain, 'meta-skill-rsi', task)
            row = read_json(path, {})
            if row.get('status') == 'complete':
                continue
            result = recover_record(root, path, task, 'meta-skill-rsi-source')
            if result:
                recovered.append(result)
            break
    for chain in manifest['target_order']:
        for arm in c.ARMS:
            if ('target', arm, chain) in occupied:
                continue
            for task in manifest['target_tasks'][chain]:
                path = c.record_path(root, chain, arm, task)
                row = read_json(path, {})
                if row.get('status') == 'complete':
                    continue
                result = recover_record(root, path, task, arm)
                if result:
                    recovered.append(result)
                break
    return recovered
