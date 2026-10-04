#!/usr/bin/env python3
"""Bounded reviews; explicitly authorized infrastructure incidents permit repair."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

PROJECT = Path('/home/ytzhang/rsi-harness')
REVIEWS = Path('/data2/ytzhang/rsi-harness-runs/codex-monitor')
CODEX = Path('/home/ytzhang/.local/bin/codex')
PROMPT = PROJECT / 'scripts/self-skill-codex-review-prompt.txt'
REPAIR_PROMPT = PROJECT / 'scripts/self-skill-codex-repair-prompt.txt'
TIMEOUT = 480


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_text(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(value)
    temporary.replace(path)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def capture(argv, timeout=15):
    try:
        result = subprocess.run(argv, cwd=PROJECT, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return {'exit_code': result.returncode, 'stdout': result.stdout[-60000:],
                'stderr': result.stderr[-2000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'exit_code': None, 'error': type(exc).__name__}


def active_root(active):
    root = Path(active['root']).resolve(strict=True)
    allowed = (Path('/data2/ytzhang/rsi-harness-runs'), PROJECT / 'runs')
    if not any(root != base and root.is_relative_to(base) for base in allowed):
        raise ValueError('active run root is outside experiment storage')
    return root


def compact(value):
    value = dict(value)
    chains = value.get('chains', {})
    value['chain_totals'] = {'total': len(chains),
                             'completed': sum(bool(c.get('complete')) for c in chains.values())}
    value['chains'] = {k: v for k, v in chains.items() if v.get('scored')}
    return value


def completed(value):
    return (value.get('state') == 'complete' and value.get('seals_valid') is True
            and value.get('total_tasks', 0) > 0
            and value.get('completed_steps') == value.get('total_tasks'))


def codex_command(summary, repair=False):
    command = [str(CODEX), '--ask-for-approval', 'never', 'exec', '--sandbox', 'workspace-write' if repair else 'read-only',
            '--config', 'model_reasoning_effort="medium"', '--ephemeral', '--json',
            '--skip-git-repo-check', '--cd', str(PROJECT),
            '--output-last-message', str(summary), '-']
    if repair:
        command[4:4] = ['--add-dir', '/data2/ytzhang/rsi-harness-runs',
                        '--add-dir', '/data2/ytzhang/rsi-harness-project-runtime-20261003',
                        '--config', 'sandbox_workspace_write.network_access=true']
    return command


def repair_needed(snapshot, root):
    authorization = read_json(root / 'recovery/autorepair-policy.json', {})
    if not authorization.get('enabled') or authorization.get('root') != str(root): return False
    manual = read_json(root / 'recovery/manual-repair.json', {})
    if manual.get('until', 0) > time.time(): return False
    status = snapshot.get('status') or {}
    if not status: return True
    if status.get('state') not in ('complete', 'paused', 'pausing') and not status.get('controller_alive', True): return True
    return status.get('state') in ('awaiting_environment', 'awaiting_inference', 'needs_attention') or (
        status.get('state') == 'running' and bool(status.get('environment_errors')))


def collect(active, root):
    result = capture([str(PROJECT / '.venv-terminal/bin/python'),
                      str(PROJECT / 'scripts/self-skill-status.py'), '--compact'], timeout=90)
    status = None
    if result['exit_code'] == 0:
        try:
            status = compact(json.loads(result['stdout']))
        except ValueError:
            pass
    if status and status.get('root') != str(root):
        raise RuntimeError('active experiment changed during snapshot; retry next scheduled review')
    snapshot = {'time': time.time(), 'active': active, 'status': status,
                'status_check_error': result if status is None else None,
                'heartbeat': read_json(root / 'heartbeat.json'),
                'watchdog': read_json(root / 'watchdog/status.json'),
                'latest_alert': read_json(root / 'alerts/latest.json'),
                'disk': {str(p): dict(zip(('total', 'used', 'free'), shutil.disk_usage(p)))
                         for p in (PROJECT, REVIEWS)},
                'gpu': capture(['nvidia-smi', '--query-gpu=index,memory.used,memory.total,utilization.gpu',
                                '--format=csv,noheader,nounits']),
                'gpu_processes': capture(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid,used_memory',
                                          '--format=csv,noheader,nounits'])}
    unit = active.get('guardian_unit', '')
    if unit.startswith('rsi-self-skill-') and unit.endswith('.service') and '/' not in unit:
        snapshot['guardian_unit'] = capture(['systemctl', '--user', 'show', unit,
            '--property=ActiveState,SubState,MainPID,ExecMainStatus,Result'])
    return snapshot


def event_receipt(path):
    value = {'thread_id': None, 'turn_completed': False, 'usage': None}
    with path.open() as events:
        for line in events:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started':
                value['thread_id'] = event.get('thread_id')
            elif event.get('type') == 'turn.completed':
                value.update(turn_completed=True, usage=event.get('usage'))
    return value


def review():
    active = read_json(PROJECT / 'runs/ACTIVE_SELF_SKILL_RSI.json')
    root = active_root(active)
    previous = read_json(REVIEWS / 'latest.json', {})
    snapshot = collect(active, root)
    manual = read_json(root / 'recovery/manual-repair.json', {})
    if manual.get('until', 0) > time.time():
        print('Foreground infrastructure repair owns this experiment; scheduled writer deferred.')
        return 0
    repair = repair_needed(snapshot, root)
    timeout = 1200 if repair else TIMEOUT
    if completed(snapshot.get('status') or {}):
        write_json(REVIEWS / 'latest.json', {'time': time.time(), 'root': str(root),
                   'state': 'skipped_campaign_complete', 'previous_review': previous.get('review_dir')})
        return 0
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    directory = REVIEWS / (stamp + '-' + str(os.getpid()))
    directory.mkdir(mode=0o700)
    snapshot_path = directory / 'snapshot.json'
    write_json(snapshot_path, snapshot)
    summary = directory / 'review.txt'
    receipt = {'started': time.time(), 'root': str(root), 'review_dir': str(directory),
               'snapshot': str(snapshot_path), 'summary': str(summary), 'state': 'reviewing',
               'read_only': not repair, 'timeout_seconds': timeout,
               'previous_review_dir': previous.get('review_dir')}
    write_json(REVIEWS / 'latest.json', receipt)
    prompt = ((REPAIR_PROMPT if repair else PROMPT).read_text() + '\n本轮宿主机快照：' + str(snapshot_path)
              + '\n当前实验目录：' + str(root)
              + '\n上一轮巡检目录（可能是另一个实验，先核对 root）：'
              + str(previous.get('review_dir')) + '\n')
    write_text(directory / 'prompt.txt', prompt)
    env = os.environ.copy()
    # A timer must create its own bounded CLI invocation, not attach to the caller.
    for key in ('CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):
        env.pop(key, None)
    with (directory / 'events.jsonl').open('w') as output, (directory / 'stderr.log').open('w') as errors:
        process = subprocess.Popen(codex_command(summary, repair), stdin=subprocess.PIPE,
            stdout=output, stderr=errors, text=True, cwd=PROJECT, env=env, start_new_session=True)
        try:
            process.communicate(prompt, timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = 124
    receipt.update(event_receipt(directory / 'events.jsonl'))
    valid = code == 0 and summary.is_file() and bool(summary.read_text().strip()) and receipt['turn_completed']
    receipt.update(finished=time.time(), exit_code=code, state='review_completed' if valid else 'review_failed')
    write_json(directory / 'receipt.json', receipt)
    write_json(REVIEWS / 'latest.json', receipt)
    if valid:
        write_text(REVIEWS / 'latest-review.txt', summary.read_text())
        write_json(REVIEWS / 'latest-success.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    return 0 if valid else (code or 1)


def main():
    os.umask(0o077)
    REVIEWS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (REVIEWS / 'review.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Previous active-experiment Codex review is still running; no duplicate started.')
            return 0
        try:
            return review()
        except Exception as exc:
            value = {'time': time.time(), 'state': 'monitor_failed',
                     'error': type(exc).__name__, 'detail': str(exc)[:500]}
            write_json(REVIEWS / 'latest-error.json', value)
            print(json.dumps(value), file=sys.stderr, flush=True)
            return 1


if __name__ == '__main__':
    sys.exit(main())
