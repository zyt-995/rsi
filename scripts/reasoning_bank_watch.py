"""Finish the two-task acceptance pilot, then maintain all seven ordered chains.

Each worker retries recognized network errors with accounting retained. This
watcher restarts only workers without a review/budget/paused terminal status;
an unknown error is preserved in supervisor-status.json for investigation.
"""
import argparse
import fcntl
from pathlib import Path
import time

from _bootstrap import REPO
from swe_chain_evo.common import atomic, read
from importlib.util import spec_from_file_location, module_from_spec

# The CLI shares the package name; load it with an explicit distinct module name.
spec = spec_from_file_location('reasoning_bank_cli', REPO/'scripts/reasoning_bank.py')
cli = module_from_spec(spec)
spec.loader.exec_module(cli)


def locked(root):
    with (root/'.campaign.lock').open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def main(root):
    root=Path(root).resolve()
    with (root/'.supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest=read(root/'manifest.json')
        pilot=Path(next(x['root'] for x in manifest['runs'] if x['chain']=='fiber'))
        started=(root/'pilot-acceptance.json').exists()
        while not (root/'HOLD').exists():
            state=read(pilot/'native-campaign.json') if (pilot/'native-campaign.json').exists() else {}
            if not started:
                if state.get('state') in ('needs_review','budget_exhausted','paused'):
                    atomic(root/'supervisor-status.json',{'state':'pilot_stopped','pilot':state,'time':time.time()})
                    return
                if state.get('state')=='pilot_completed':
                    evidence=[read(pilot/p) for p in ('first-task-acceptance.json','first-update-acceptance.json','memory-use-acceptance.json')]
                    if not all(p.get('passed') for p in evidence):
                        raise RuntimeError('Pilot acceptance is incomplete')
                    atomic(root/'pilot-acceptance.json',{'passed':True,'two_formal_tasks':True,
                         'two_official_updates':len(state['arms']['reasoning-bank']['updates'])==2,
                         'memory_injected_on_second_task':True,'time':time.time()})
                    started=True
                    cli.launch(root)
                elif not locked(pilot):
                    cli.launch(root,'fiber',2)
            else:
                resumable=[];summary=[]
                for row in manifest['runs']:
                    run=Path(row['root']);p=run/'native-campaign.json'
                    s=read(p) if p.exists() else {}
                    busy=locked(run)
                    summary.append({'chain':row['chain'],'state':s.get('state','prepared'),'worker_active':busy,
                                    'completed':len(s.get('arms',{}).get('reasoning-bank',{}).get('rows',[])),
                                    'error':s.get('error')})
                    if not busy and s.get('state') not in ('completed','needs_review','budget_exhausted','paused'):
                        resumable.append(row['chain'])
                atomic(root/'supervisor-status.json',{'state':'running' if any(x['worker_active'] for x in summary) or resumable else 'needs_review',
                      'chains':summary,'time':time.time()})
                for chain in resumable:
                    cli.launch(root,chain)
                if all(x['state']=='completed' for x in summary):
                    cli.status(root)
                    atomic(root/'supervisor-status.json',{'state':'completed','chains':summary,'time':time.time()})
                    return
                if all(x['state'] in ('completed','needs_review','budget_exhausted','paused') for x in summary):
                    cli.status(root)
                    return
            cli.status(root)
            time.sleep(30)
        atomic(root/'supervisor-status.json',{'state':'paused','time':time.time()})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign',required=True,type=Path)
    main(parser.parse_args().campaign)
