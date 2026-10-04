"""Audited dispatcher-only handoff; preserve/adopt every existing source worker."""
import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cross_chain_rsi import campaign as c
from rsi_harness.clean_rsi import read_json
from rsi_harness.terminal_infra import atomic_json, file_hash


def handoff(root):
    c.verify(root)
    with c.legacy.exclusive(root / 'handoff.lock'):
        existing = read_json(root / 'supervisor_launch.json')
        if existing and c.legacy.alive(existing):
            return {'status': 'already_running', 'supervisor': existing}
        spec = importlib.util.spec_from_file_location('resize_helpers', c.PROJECT / 'scripts/resize-meta-skill-workers.py')
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)
        if not (c.SOURCE_ROOT / 'PAUSE').exists():
            raise RuntimeError('old 21-chain dispatcher must be paused')
        launch = read_json(c.SOURCE_ROOT / 'supervisor_launch.json')
        pid = helpers.owned_supervisor(c.SOURCE_ROOT, launch) if launch and c.legacy.alive(launch) else None
        stopped, retired = False, False
        audit = root / 'handoff' / str(time.time_ns())
        audit.mkdir(parents=True)
        try:
            if pid:
                os.kill(pid, signal.SIGSTOP)
                stopped = True
                for _ in range(100):
                    state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
                    if state in ('T', 't'):
                        break
                    time.sleep(.02)
                else:
                    raise RuntimeError('dispatcher did not suspend')
            active = read_json(c.SOURCE_ROOT / 'active.json', {})
            live = helpers.live_stage_pids(c.SOURCE_ROOT)
            if live - {w['pid'] for w in active.values()}:
                raise RuntimeError('worker launched but not checkpointed; resume and inspect')
            if any(w['chain'] not in c.SOURCES for w in active.values() if c.legacy.alive(w)):
                raise RuntimeError('source worker outside four-chain cohort')
            before = {str(p): file_hash(p) for p in c.SOURCE_ROOT.glob('*/meta-skill-rsi/stages/*/record.json')
                      if read_json(p).get('status') == 'complete'}
            atomic_json(audit / 'source_supervisor.json', launch)
            atomic_json(audit / 'source_active.json', active)
            atomic_json(audit / 'completed_records.json', before)
            adopted = {port: {**w, 'kind': 'source', 'arm': 'meta-skill-rsi'} for port, w in active.items() if c.legacy.alive(w)}
            # Never replace a prior cross-chain active checkpoint on recovery.
            old_new_active = read_json(root / 'active.json', {})
            for port, worker in old_new_active.items():
                if port in adopted and worker['pid'] != adopted[port]['pid'] and c.legacy.alive(worker):
                    raise RuntimeError('port conflict during recovery')
                if port not in adopted:
                    adopted[port] = worker
            atomic_json(root / 'active.json', adopted)
            if pid:
                helpers.owned_supervisor(c.SOURCE_ROOT, launch)
                os.kill(pid, signal.SIGKILL)  # dispatcher PID ONLY; workers have separate sessions
                retired = True
                for _ in range(100):
                    if not c.legacy.alive(launch):
                        break
                    time.sleep(.02)
                else:
                    raise RuntimeError('dispatcher did not retire')
            with (root / 'supervisor.log').open('ab', buffering=0) as stream:
                process = subprocess.Popen([sys.executable, '-u', '-m', 'cross_chain_rsi.campaign', '--root', str(root), '--run'],
                                           cwd=c.PROJECT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            successor = {'pid': process.pid, 'identity': c.core.process_identity(process.pid), 'started': time.time(), 'root': str(root)}
            atomic_json(root / 'supervisor_launch.json', successor)
            if any(file_hash(Path(p)) != h for p, h in before.items()):
                raise RuntimeError('completed source record changed')
            report = {'status': 'handoff_started', 'supervisor': successor, 'adopted': adopted,
                      'completed_preserved': len(before), 'source_scope': list(c.SOURCES), 'audit': str(audit)}
            atomic_json(audit / 'result.json', report)
            return report
        except BaseException:
            if stopped and not retired and c.legacy.alive(launch):
                os.kill(pid, signal.SIGCONT)
            atomic_json(audit / 'needs_review.json', {'time': time.time(), 'dispatcher_retired': retired})
            raise


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(handoff(a.root.resolve()), ensure_ascii=False, indent=2))
