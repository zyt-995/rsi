"""Independent ordered chain with official per-task ReasoningBank updates."""
import argparse
import fcntl
import json
from pathlib import Path
import shutil
import threading
import time
import traceback

from swe_chain_evo.common import atomic, read, sha
from methods.evo_fifty.gateway import Broker, Server, TransientProviderError
from methods.evo_fifty.spend import SpendBlocked
from methods.evo_fifty.trial import NetworkUnavailable
from evoskill_swe.campaign import acceptance
from .trial import TrialRunner
from .transport import Clients
from . import native

ARM = 'reasoning-bank'


def check_pins(root):
    for filename, digest in read(Path(root) / 'source-pins.json').items():
        if not Path(filename).is_file() or sha(filename) != digest:
            raise RuntimeError('Frozen experiment input changed: ' + filename)


def receipts(root):
    return [read(p) for base in [root, *(root / 'channels').glob('*')]
            for p in (base / 'provider-calls').glob('*/request.json')]


def report(root, state, persist=True):
    calls = receipts(root)
    if len({c['id'] for c in calls}) != len(calls):
        raise RuntimeError('Duplicate receipt IDs')
    settled = [c for c in calls if c['state'] == 'settled']
    def usage(values):
        return {'input':sum(c['usage']['prompt_tokens'] for c in values),
                'output':sum(c['usage']['completion_tokens'] for c in values)}
    formal = [c for c in settled if c['phase'] == 'formal']
    rsi = [c for c in settled if c['phase'] != 'formal']
    unknown = [c for c in calls if c['state'] in ('started','requires_review')]
    rows = state['arms'][ARM]['rows']; updates = state['arms'][ARM]['updates']
    m = dict(method=ARM, chain=root.name, completed=len(rows), passed=sum(r['reward'] for r in rows),
         step_accuracy=sum(r['reward'] for r in rows)/len(rows) if rows else None,
         chain_accuracy=int(all(r['reward']==1 for r in rows)) if len(rows)==15 else None,
         task_tokens=usage(formal), rsi_tokens=usage(rsi), total_tokens=usage(settled),
         phase_tokens={p:usage([c for c in settled if c['phase']==p]) for p in sorted({c['phase'] for c in settled})},
         cost_usd=sum(c['usage']['cost'] for c in settled), updates_completed=len(updates),
         memory_experiences=len(updates), unknown_usage_calls=[c['id'] for c in unknown],
         unknown_token_upper_bound=sum(c['token_reservation'] for c in unknown),
         usage_complete=not unknown, status=state['state'])
    if persist:
        atomic(root / 'metrics.json', {'arms':[m], 'state':state['state'], 'equal_total_token_budget_comparison':False})
    return m


def retrieve(root, clients, stream, bank, index, folder):
    path = folder / 'retrieval/selection.json'
    if path.exists():
        value = read(path)
        if value['bank_sha256'] != sha(root / 'memory.json') or sha(value['note']) != value['note_sha256']:
            raise RuntimeError('Retrieval checkpoint inputs changed')
        return Path(value['note'])
    out = folder / 'retrieval'; out.mkdir(parents=True, exist_ok=True)
    cache = out / 'embeddings.jsonl'
    # Retrying native screening must begin from the same pre-task cache. Cached
    # embedding responses are reused; its append operation cannot duplicate IDs.
    initial = out / 'input-embeddings.jsonl'
    if not initial.exists():
        prior = root / 'embeddings.jsonl'
        initial.write_bytes(prior.read_bytes() if prior.exists() else b'')
    shutil.copy2(initial, cache)
    clients.embed_sequence = 0
    selected, text = native.retrieval(bank, stream[index]['instruction'], stream[index]['task'], cache,
        lambda text, kind, dimensions: clients.embed(index, out, text, kind, dimensions))
    if any(e['index'] >= index for e in selected):
        raise RuntimeError('Future/self experience retrieved')
    note = out / 'memory.md'; note.write_text(text)
    value = {'index':index, 'bank_sha256':sha(root / 'memory.json'),
             'selected_tasks':[e['task_id'] for e in selected], 'selected_indices':[e['index'] for e in selected],
             'note':str(note), 'note_sha256':sha(note), 'cache_sha256':sha(cache),
             'official_source':native.source_evidence()}
    atomic(path, value)
    return note


def learn(root, clients, stream, rows, bank, index, folder):
    out = folder / 'update'; out.mkdir(parents=True, exist_ok=True)
    path = out / 'outcome.json'
    if path.exists():
        value = read(path)
        if value['input_bank_sha256'] != sha(root / 'memory.json'):
            # After commit the saved outcome remains valid if exactly its entry is present.
            if bank and bank[-1] == value['entry']:
                return value
            raise RuntimeError('Memory update checkpoint changed')
        return value
    row = rows[index]
    if sha(row['result']) != row['result_sha256'] or row.get('exception'):
        raise RuntimeError('Public formal result unavailable or changed')
    trace = Path(row['result']).parent / 'agent/trajectory.json'
    text = native.public_trajectory(trace)
    # Full public trajectory, no scalar benchmark score or hidden verifier output.
    status, items = native.judge_and_extract(stream[index]['instruction'], text, clients.config['model']['id'],
         lambda role, model, contents, options: clients.generate(out, role, model, contents, options))
    if not any(isinstance(item,str) and item.strip() for item in items):
        raise RuntimeError('No memory content extracted; paid response retained')
    entry = dict(task_id=stream[index]['task'], query=stream[index]['instruction'], memory_items=items,
                 status='success' if status else 'fail', index=index)
    value = dict(index=index, input_bank_sha256=sha(root/'memory.json'), trace_sha256=sha(trace),
                 entry=entry, official_status=status, original_markdown_parser=True,
                 execution={'passed':True,'logical_judgments':1,'logical_extractions':1,'extra_task_rollouts':0},
                 finished=time.time())
    atomic(path, value)
    return value


def run(root, pilot_tasks=None):
    root = Path(root).resolve(); cfg=read(root/'configuration.json')
    session=Path(cfg['session_root'])
    grant=read(session/'RUN_AUTHORIZATION.json')
    if not grant.get('authorized') or grant['configuration_hashes'].get(str(root)) != sha(root/'configuration.json'):
        raise RuntimeError('This exact run is not authorized')
    check_pins(root)
    with (root/'.campaign.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        key=Path(cfg['credential_file']).read_text().strip()
        broker=Broker(root,key,cfg); clients=Clients(root,key,cfg)
        runner=TrialRunner(root,broker,cfg['runtime']['gateway_port'])
        runner.pins=read(root/'source-pins.json')
        stream=read(root/'task-stream.json')
        state=read(root/'native-campaign.json') if (root/'native-campaign.json').exists() else {
            'state':'ready','arms':{ARM:{'rows':[],'updates':[],'status':'ready'}}}
        item=state['arms'][ARM]
        server=Server(('127.0.0.1',runner.port),broker)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        def save(status):
            state.update(state=status,updated=time.time())
            item['status']=status
            atomic(root/'native-campaign.json',state)
            report(root,state)
        try:
            while len(item['rows'])<len(stream) or len(item['updates'])<len(item['rows']):
                if (session/'HOLD').exists() or (root/'PAUSE').exists():
                    save('paused');return
                check_pins(root)
                bank=read(root/'memory.json')
                updating=len(item['updates'])<len(item['rows'])
                index=len(item['updates']) if updating else len(item['rows'])
                if not updating and pilot_tasks and index>=pilot_tasks:
                    save('pilot_completed');return
                folder=root/'arms'/ARM/'steps'/f'{index:06d}'
                state['active']={'index':index,'phase':'update' if updating else 'formal'}
                save('running')
                try:
                    if updating:
                        value=learn(root,clients,stream,item['rows'],bank,index,folder)
                        if len(bank)==index:
                            bank.append(value['entry']);atomic(root/'memory.json',bank)
                        elif len(bank)!=index+1 or bank[-1]!=value['entry']:
                            raise RuntimeError('Chronological memory commit mismatch')
                        shutil.copy2(folder/'retrieval/embeddings.jsonl',root/'embeddings.jsonl')
                        item['updates'].append(value)
                        atomic(root/'first-update-acceptance.json',{'passed':True,'index':index,
                             'official_self_judge':True,'source':native.source_evidence()}) if index==0 else None
                    else:
                        if len(bank)!=index:
                            raise RuntimeError('Memory bank does not match completed chronological prefix')
                        note=retrieve(root,clients,stream,bank,index,folder)
                        value=runner.run(ARM,index,folder/'formal',note=note)
                        if value.get('exception'):
                            raise RuntimeError('Infrastructure/agent exception retained; not counted as an algorithm failure')
                        if index==0:
                            acceptance(root,runner,value)
                        if index>0 and note.read_text():
                            injection=Path(value['result']).parent/'agent/reasoning-bank-injection.json'
                            proof=read(injection)
                            if not proof['nonempty'] or proof['memory_sha256']!=sha(note):
                                raise RuntimeError('Retrieved memory was not injected')
                            atomic(root/'memory-use-acceptance.json',{'passed':True,'index':index,
                                   'injection_sha256':sha(injection),'memory_sha256':sha(note)})
                        item['rows'].append(value)
                    state.pop('error',None);save('running')
                except (NetworkUnavailable,TransientProviderError) as exc:
                    state['error']={'type':type(exc).__name__,'message':str(exc)}
                    save('waiting_for_network');time.sleep(60)
            state.pop('active',None);save('completed')
        except BaseException as exc:
            state['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
            save('budget_exhausted' if isinstance(exc,SpendBlocked) else 'needs_review')
            raise
        finally:
            server.shutdown();server.server_close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--pilot-tasks',type=int)
    a=p.parse_args();run(a.root,a.pilot_tasks)
