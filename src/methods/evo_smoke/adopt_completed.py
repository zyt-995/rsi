"""Audit and adopt a scored trial after a controller amendment; never replay."""
import json
from pathlib import Path
import time

from methods.evo_trial import sha
from methods.modular_update import tree_hashes
from .trial import atomic


def adopt(root,output,modules=None,candidate=None):
    root,output=Path(root),Path(output)
    if (output/'summary.json').exists():return json.loads((output/'summary.json').read_text())
    launch=json.loads((output/'launch.json').read_text())
    path=next((output/'trials').glob('*/result.json'))
    raw=json.loads(path.read_text())
    reward=((raw.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    if reward not in (0,1):raise RuntimeError('trial not scored; cannot adopt')
    # The worker implementation was identical throughout this controller fix.
    saved=root/'native-adapter-snapshot/agents.py'
    current=Path(__file__).parent/'agents.py'
    if sha(saved)!=sha(current):raise RuntimeError('worker source changed; separate audit required')
    scope=next(s for s in json.loads((root/'gateway-scopes.json').read_text()).values()
               if s['name']==str(output.relative_to(root)))
    if scope['blocked'] or scope['pending_tokens']:raise RuntimeError('unreconciled scope')
    requests=[json.loads((root/'provider-calls'/i/'request.json').read_text()) for i in scope['calls']]
    if not all(r['state']=='settled' for r in requests):raise RuntimeError('unsettled receipt')
    fingerprint={'candidate_sha256':sha(Path(candidate)) if candidate else None,
                 'modules_hashes':tree_hashes(Path(modules)) if modules else None,
                 'composer_name':'llm_dynamic','composer_scope':'all','locked_module_type':None}
    if candidate:
        fingerprint['prompt_templates']=tree_hashes(Path(candidate).parent.parent/'prompt-templates')
        fingerprint['agents_source']=tree_hashes(Path(candidate).parent)
    result={'arm':launch['arm'],'index':launch['index'],'phase':launch['phase'],'task':launch['entry']['task'],
        'source_fingerprint':fingerprint,'reward':reward,'result':str(path),'result_sha256':sha(path),
        'input_tokens':sum(r['usage']['prompt_tokens'] for r in requests),
        'output_tokens':sum(r['usage']['completion_tokens'] for r in requests),
        'cost_usd':sum(r['usage']['cost'] for r in requests),'call_ids':scope['calls'],
        'exception':(raw.get('exception_info') or {}).get('exception_type') or
             (((raw.get('agent_result') or {}).get('metadata') or {}).get('agent_exception') or {}).get('type'),
        'returncode':0,'time':time.time(),'source_amendment_recovery':'scored original result and settled usage; no model replay'}
    atomic(output/'adoption-audit.json',{'raw_result_sha256':sha(path),'worker_sha256':sha(current),
                                      'agent_replayed':False,'call_ids':scope['calls'],'reward':reward})
    atomic(output/'summary.json',result)
    return result
