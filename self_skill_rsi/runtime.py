"""Unchanged native solver, explicit budgets, no silent trial retries."""
from contextlib import ExitStack, contextmanager
import subprocess
import threading
import time
from pathlib import Path

from cross_chain_rsi import admission, campaign as cross
from rsi_harness import clean_campaign as core, meta_skill_campaign as legacy
from rsi_harness.clean_rsi import SOLVE, digest, read_json, select_skills
from rsi_harness.terminal_infra import atomic_json, file_hash, fingerprint
from skill_repair_v2.campaign import DEFAULT as REPAIR_ROOT, OLD as CROSS_ROOT
from . import bank

# The read-only legacy compatibility adapter temporarily replaces a module
# function. Serialize that small audit, not native solves, across our lanes.
ADMISSION_LOCK = threading.Lock()


def check_entry(entry):
    from harbor.models.task.task import Task
    path = Path(entry['path'])
    if Task(path).checksum != entry['task_checksum'] or fingerprint(path) != entry['environment_fingerprint']:
        raise RuntimeError('registered_task_or_environment_changed:' + entry['task'])
    if entry.get('readiness_root'):
        ok, reason = core.ready(entry['task'], Path(entry['readiness_root']))
        if not ok or cross.image_id(entry['image']) != entry['image_id']:
            raise RuntimeError('environment_not_ready:' + str(reason))


def check_prior_controllers():
    """Legacy dispatchers do not all honor our port locks: fail closed, not race."""
    for root in (REPAIR_ROOT, CROSS_ROOT, cross.SOURCE_ROOT, legacy.CONTROL, core.OLD_ROOT):
        for name in ('status.json', 'watchdog/status.json', 'supervisor_launch.json', 'watchdog_launch.json'):
            value = read_json(root / name, {})
            pid = value.get('pid', value.get('supervisor_pid'))
            if pid and core.process_identity(pid) and (
                    not value.get('identity') or core.process_identity(pid) == value['identity']):
                raise RuntimeError('existing_controller_or_watchdog_live:' + str(root / name))


@contextmanager
def inference_lease(ports):
    if not ports or len(ports) != len(set(ports)) or set(ports) - set(cross.PORTS):
        raise ValueError('invalid_endpoint_list')
    check_prior_controllers()
    with ExitStack() as stack:
        for port in sorted(ports):
            stack.enter_context(legacy.exclusive(core.PROJECT / 'runs' / f'meta-port-{port}.lock'))
            if not core.endpoint_ok(port) or not legacy.endpoint_idle(port):
                raise RuntimeError('endpoint_not_ready_or_in_use:' + str(port))
        check_prior_controllers()
        yield


def native_solve(entry, store, head, folder, port, purpose):
    if purpose not in ('stream', 'validation', 'probe'):
        raise ValueError('unknown_trial_purpose')
    check_entry(entry)
    payload, _ = bank.load(store, head)
    if read_json(store / 'HEAD.json') != head:
        raise RuntimeError('trial_store_not_pinned_to_expected_version')
    disk = core.docker_disk_status(core.PROJECT)
    if not disk['ok']:
        raise RuntimeError('disk_admission_failed:' + str(disk))
    folder.mkdir(parents=True, exist_ok=True)
    plan = {'uid': entry['uid'], 'task_checksum': entry['task_checksum'], 'before': head, 'purpose': purpose}
    existing = read_json(folder / 'plan.json')
    if existing is not None and existing != plan:
        raise RuntimeError('trial_plan_changed')
    atomic_json(folder / 'plan.json', plan)
    paths = list((folder / 'trials').glob('*/result.json'))
    if not paths:
        if (folder / 'launch.json').exists():
            raise RuntimeError('interrupted_trial_not_repeated')
        command = core.harbor_command(Path(entry['path']), folder / 'trials', 'skill-rsi', store)
        atomic_json(folder / 'launch.json', {'command': command, 'port': port, 'started': time.time(),
                                           'formal_score': purpose in ('stream', 'probe'), **plan})
        with (folder / 'worker.log').open('ab', buffering=0) as stream:
            child = subprocess.Popen(command, cwd=core.PROJECT, env=core.runner_environment(port),
                                     stdout=stream, stderr=subprocess.STDOUT)
            atomic_json(folder / 'child.json', {'pid': child.pid, 'identity': core.process_identity(child.pid)})
            try:
                child.wait(timeout=10800)
            except subprocess.TimeoutExpired:
                raise RuntimeError('native_trial_watchdog; live_child_retained_for_inspection')
        paths = list((folder / 'trials').glob('*/result.json'))
    if len(paths) != 1:
        raise RuntimeError('missing_or_ambiguous_trial')
    path = paths[0]
    with ADMISSION_LOCK:
        issues = admission.compatibility(path, entry['task'], folder)
    if issues:
        raise RuntimeError('invalid_native_result:' + str(issues))
    start = read_json(path.parent / 'agent/rsi_start.json', {})
    end = read_json(path.parent / 'agent/rsi_end.json', {})
    chosen, _ = select_skills(payload, Path(entry['path']).joinpath('instruction.md').read_text())
    if start.get('version') != head or start.get('arm') != 'skill-rsi' or start.get('selected_skills') != chosen:
        raise RuntimeError('incorrect_skill_version_or_retrieval')
    if (type(end.get('model_calls')) is not int or not 1 <= end['model_calls'] <= SOLVE['max_model_calls']
            or type(end.get('commands')) is not int or not 0 <= end['commands'] <= SOLVE['max_commands']):
        raise RuntimeError('missing_or_invalid_solver_budget_accounting')
    if read_json(store / 'HEAD.json') != head or bank.load(store, head)[0] != payload:
        raise RuntimeError('skills_changed_during_trial')
    data = read_json(path)
    reward = data['verifier_result']['rewards']['reward']
    if reward not in (0, 1):
        raise RuntimeError('unexpected_reward_scale')
    artifacts = [path, path.parent / 'agent/trajectory.json',
                 path.parent / 'agent/rsi_start.json', path.parent / 'agent/rsi_end.json']
    artifacts += [path.parent / name for name in ('agent/rsi_events.jsonl', 'verifier/ctrf.json',
                  'verifier/test-stdout.txt', 'verifier/reward.txt') if (path.parent / name).exists()]
    result = {'status': 'complete', 'purpose': purpose, 'uid': entry['uid'], 'task': entry['task'],
        'result': str(path), 'reward': reward, 'before': head, 'selected_skills': chosen, 'runtime': end,
        'solve_seconds': path.stat().st_mtime - read_json(folder / 'launch.json')['started'],
        'solve_usage': {k: (data.get('agent_result') or {}).get(k) for k in ('n_input_tokens', 'n_output_tokens', 'n_cache_tokens')},
        'hashes': {str(p): file_hash(p) for p in artifacts}}
    prior_result = read_json(folder / 'record.json')
    if prior_result is not None and prior_result != result:
        raise RuntimeError('saved_trial_record_changed')
    atomic_json(folder / 'record.json', result)
    return result
