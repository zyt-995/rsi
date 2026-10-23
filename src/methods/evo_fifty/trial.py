"""Fresh Evo trials: same images, grader, model envelope, and billing gateway."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

from methods.evo_trial import command as legacy_command, with_grader, sha, PROJECT
from methods.comparison import read, DEFAULT_CONTRACT
from rsi_portable.paths import runner_environment
from .gateway import transport_failure_record


class NetworkUnavailable(RuntimeError):
    """A transport recovery window ended; the coordinator may retry after a wait."""


def atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    os.replace(temp, path)


class TrialRunner:
    def __init__(self, root, broker, port=8050):
        self.root, self.broker, self.port = Path(root), broker, port
        self.config = broker.config
        self.contract = read(DEFAULT_CONTRACT)
        self.stream = read(self.root / 'task-stream.json')
        self.by_task = {entry['task']: i for i, entry in enumerate(self.stream)}
        self.env = runner_environment(port)
        self.pins = None

    def check_pins(self):
        if self.pins:
            for path, expected in self.pins.items():
                if sha(Path(path)) != expected:
                    raise ValueError('pinned source/config changed: '+path)

    def transport_failure(self, output):
        """Identify a finished trial interrupted by a quarantined network call."""
        output=Path(output)
        if not (output/'launch.json').exists() or (output/'summary.json').exists():return None
        paths=list((output/'trials').glob('*/result.json'))
        if len(paths)!=1:return None
        result=read(paths[0])
        if not result.get('finished_at'):return None
        name=str(output.relative_to(self.root))
        scopes=[s for s in self.broker.scopes.values() if s['name']==name]
        if len(scopes)!=1 or not scopes[0]['blocked']:return None
        receipts=[read(self.root/'provider-calls'/cid/'request.json') for cid in scopes[0]['calls']]
        uncertain=[r for r in receipts if r['state'] in ('requires_review','rejected') or r.get('retryable_transport')]
        def is_network(row):
            response=self.root/'provider-calls'/row['id']/'response.json'
            return transport_failure_record(row, read(response) if response.exists() else None)
        if not uncertain or not all(is_network(r) for r in uncertain):return None
        if any(r['state'] not in ('settled','requires_review','rejected') for r in receipts):return None
        return {'trial':str(output),'raw_result':str(paths[0]),'raw_result_sha256':sha(paths[0]),
                'call_ids':scopes[0]['calls'],'uncertain_call_ids':[r['id'] for r in uncertain],
                'reason':'network interruption; incomplete attempt retained for cost accounting',
                'all_costs_retained':True}

    def run(self, arm, index, output, phase='formal', **kwargs):
        output=Path(output)
        # Recover infrastructure interruptions for every arm. Normal reward=0
        # results are final. Formal restarts share the original task token cap.
        retries=self.config['runtime'].get(
            'formal_transport_retries' if phase=='formal' else 'validation_transport_retries',0)
        recovery=output/'transport-recovery.json'
        saved=read(recovery) if recovery.exists() else {}
        failures=saved.get('failures',[])
        epoch=self.config['runtime'].get('validation_transport_epoch')
        if epoch and (not isinstance(epoch,str) or not all(c.isalnum() or c=='-' for c in epoch)):
            raise ValueError('invalid transport recovery epoch')
        if (output/'summary.json').exists():
            return self._run_once(arm,index,output,phase=phase,**kwargs)
        start=saved.get('next_attempt',0) if saved.get('transport_epoch')==epoch else 0
        for attempt in range(start,start+retries+1):
            retry_root=output/'transport-retries'
            if epoch:retry_root=retry_root/epoch
            target=output if attempt==0 else retry_root/f'{attempt:03d}'
            failed=self.transport_failure(target) if retries else None
            if failed is None:
                try:
                    run_kwargs=dict(kwargs)
                    prior_ids=list(dict.fromkeys(cid for f in failures for cid in f['call_ids'])) if phase=='formal' else []
                    if (phase=='formal' and attempt and prior_ids
                            and self.config['solve']['max_input_plus_output_tokens'] is not None):
                        prior=[read(self.root/'provider-calls'/cid/'request.json') for cid in prior_ids]
                        spent_bound=sum(
                            r['usage']['prompt_tokens']+r['usage']['completion_tokens'] if r['state']=='settled'
                            else r['token_reservation'] if r['state']=='requires_review' else 0 for r in prior)
                        remaining=self.config['solve']['max_input_plus_output_tokens']-spent_bound
                        if remaining<=0:
                            from .spend import SpendBlocked
                            raise SpendBlocked('formal task budget exhausted across infrastructure attempts')
                        run_kwargs['scope_limit']=remaining
                    value=self._run_once(arm,index,target,phase=phase,**run_kwargs)
                    if attempt:
                        value={**value,'transport_retry_count':attempt,'transport_failures':failures}
                        if phase=='formal':
                            ids=list(dict.fromkeys(prior_ids+value['call_ids']))
                            receipts=[read(self.root/'provider-calls'/cid/'request.json') for cid in ids]
                            usages=[r['usage'] for r in receipts if r['state']=='settled']
                            uncertain=[r['id'] for r in receipts if r['state']=='requires_review']
                            value.update(call_ids=ids,
                                input_tokens=sum(u['prompt_tokens'] for u in usages),
                                output_tokens=sum(u['completion_tokens'] for u in usages),
                                cost_usd=sum(u['cost'] for u in usages),
                                usage_complete=not uncertain,uncertain_call_ids=uncertain,
                                infrastructure_interrupted=True,
                                formal_token_cap_includes_all_attempts=True)
                        atomic(output/'summary.json',value)
                    return value
                except RuntimeError:
                    failed=self.transport_failure(target) if retries else None
                    if failed is None:raise
            if not any(f['trial']==failed['trial'] for f in failures):failures.append(failed)
            atomic(output/'transport-recovery.json',{'failures':failures,
                    'failed_trials_retained':True,'formal_attempts_unchanged':phase!='formal',
                    'formal_recovery_policy':'same token cap across all attempts; normal task failures never retried',
                    'transport_epoch':epoch,
                    'next_attempt':attempt+1,
                    'next_retry':attempt+1 if attempt<start+retries else None})
            if attempt<start+retries:time.sleep(min(2**min(attempt,3),8))
        raise NetworkUnavailable(f'{phase} transport recovery waiting for service; retained attempts and charges')

    def _run_once(self, arm, index, output, phase='formal', modules=None, candidate=None,
            composer_name='llm_dynamic', locked_module_type=None, composer_scope='all', scope_limit=None):
        output = Path(output)
        self.check_pins()
        from methods.modular_update import tree_hashes
        source_fingerprint = {'candidate_sha256': sha(Path(candidate)) if candidate else None,
                              'modules_hashes': tree_hashes(Path(modules)) if modules else None,
                              'composer_name': composer_name, 'composer_scope': composer_scope,
                              'locked_module_type': locked_module_type}
        if candidate:
            source_fingerprint['prompt_templates'] = tree_hashes(Path(candidate).parent.parent/'prompt-templates')
            source_fingerprint['agents_source'] = tree_hashes(Path(candidate).parent)
        cached = output / 'summary.json'
        if cached.exists():
            value = read(cached)
            if value['arm'] != arm or value['index'] != index or value['phase'] != phase:
                raise ValueError('cached trial identity changed')
            if value['result'] and sha(Path(value['result'])) != value['result_sha256']:
                raise ValueError('immutable trial changed')
            if value.get('grading_continuation') and sha(Path(value['grading_continuation'])) != value['grading_continuation_sha256']:
                raise ValueError('grading continuation changed')
            if value.get('source_fingerprint') != source_fingerprint:
                raise ValueError('cached solver source changed')
            return value
        if (output / 'launch.json').exists():
            raise RuntimeError('partial trial; preserve paid calls, do not replay')
        entry = with_grader(self.contract, index)
        from harbor.models.task.task import Task
        if Task(Path(entry['path'])).checksum != entry['task_checksum']:
            raise ValueError('task changed')
        inspected = subprocess.check_output(['docker', 'image', 'inspect', entry['image_id'],
                   '--format', '{{.Id}}'], env=self.env, text=True, timeout=20).strip()
        if inspected != entry['image_id']:
            raise ValueError('image changed')
        family = 'meta-harness' if arm in ('meta-harness', 'kira-static') else 'modularrsi'
        import_path = ('methods.evo_online.agents:CandidateAudited' if candidate else
                       'methods.evo_online.agents:KiraAudited' if family == 'meta-harness' else
                       'methods.evo_online.agents:ModularAudited')
        cmd = legacy_command(family, entry, output, modules, import_path, composer_name)
        model = self.config['model']
        info = {'max_input_tokens': model['context_tokens'] - model['max_output_tokens'],
                'max_output_tokens': model['max_output_tokens'],
                'input_cost_per_token': 0.0, 'output_cost_per_token': 0.0}
        for i, part in enumerate(cmd):
            if part == '--model': cmd[i+1] = 'openai/' + model['id']
            if part == '--agent-kwarg':
                name, val = cmd[i+1].split('=', 1)
                if name == 'max_turns': val = str(self.config['solve']['max_agent_turns'])
                if name == 'api_base': val = f'http://127.0.0.1:{self.port}/v1'
                if name == 'model_info': val = json.dumps(info)
                if name == 'llm_call_kwargs': val = json.dumps({'max_tokens': model['max_output_tokens'], 'timeout': 2700})
                cmd[i+1] = name + '=' + val
            if part in ('--ae', '--ve') and ':17897' in cmd[i+1]:
                cmd[i+1] = cmd[i+1].replace(':17897', ':17898')
        cmd += ['--agent-kwarg', 'temperature=' + str(model['temperature'])]
        if family == 'meta-harness': cmd += ['--agent-kwarg', 'model_info=' + json.dumps(info)]
        else:
            cmd += ['--agent-kwarg', 'composer_scope=' + composer_scope]
            if locked_module_type: cmd += ['--agent-kwarg', 'locked_module_type=' + locked_module_type]
        output.mkdir(parents=True, exist_ok=True)
        token = self.broker.scope(arm, phase, str(output.relative_to(self.root)), limit=scope_limit)
        env = dict(self.env)
        for key in ('OPENROUTER_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY'):
            env.pop(key, None)
        env.update(OPENAI_API_KEY=token, OPENAI_BASE_URL=f'http://127.0.0.1:{self.port}/v1',
                   EVO_ONLINE_API_BASE=f'http://127.0.0.1:{self.port}/v1',
                   PYTHONPATH=os.pathsep.join([str(PROJECT), str(PROJECT/'methods/vendor/meta-harness/reference_examples/terminal_bench_2'), env.get('PYTHONPATH', '')]),
                   TMPDIR=str(self.root / 'tmp'), PYTHONDONTWRITEBYTECODE='1')
        Path(env['TMPDIR']).mkdir(exist_ok=True)
        if candidate: env['EVO_ONLINE_CANDIDATE'] = str(Path(candidate).resolve())
        atomic(output / 'launch.json', {'arm': arm, 'index': index, 'phase': phase, 'entry': entry,
                                      'command': cmd, 'time': time.time()})
        with (output/'worker.log').open('ab') as log:
            proc = subprocess.run(cmd, cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=8400)
        self.check_pins()
        if candidate and (sha(Path(candidate)) != source_fingerprint['candidate_sha256'] or
            tree_hashes(Path(candidate).parent.parent/'prompt-templates') != source_fingerprint['prompt_templates'] or
            tree_hashes(Path(candidate).parent) != source_fingerprint['agents_source']):
            raise RuntimeError('candidate source changed during a trial')
        paths = list((output/'trials').glob('*/result.json'))
        result = read(paths[0]) if len(paths) == 1 else None
        scope = self.broker.scopes[token]
        billed = []
        for call_id in scope['calls']:
            p = self.root/'provider-calls'/call_id/'request.json'
            billed.append(read(p))
        usage = [(row.get('usage') or {}) for row in billed if row['state'] == 'settled']
        over_bound = any(state == 'over_bound' for _,state in self.broker.ledger.snapshot()['blocking'])
        reward = (((result or {}).get('verifier_result') or {}).get('rewards') or {}).get('reward')
        incomplete=[r for r in billed if r['state'] in ('requires_review','rejected') or r.get('retryable_transport')]
        def network(row):
            response=self.root/'provider-calls'/row['id']/'response.json'
            return transport_failure_record(row,read(response) if response.exists() else None)
        network_only=bool(incomplete) and all(network(r) for r in incomplete)
        recovered_network=network_only and (not scope['blocked'] or reward==1)
        if over_bound or ((scope['blocked'] or scope['pending_tokens']) and not recovered_network):
            raise RuntimeError('trial provider accounting requires review')
        reason = str(((result or {}).get('agent_result') or {}).get('metadata', {}).get('budget_stop_reason') or '')
        if 'global dollar limit' in reason:
            from .spend import SpendBlocked
            raise SpendBlocked('global limit stopped an incomplete formal/update trial')
        if reward not in (0, 1):
            raise RuntimeError('unscored trial; inspect artifacts instead of replay')
        value = {'arm': arm, 'index': index, 'phase': phase, 'task': entry['task'],
                 'source_fingerprint': source_fingerprint,
                 'reward': reward, 'result': str(paths[0]), 'result_sha256': sha(paths[0]),
                 'input_tokens': sum(u['prompt_tokens'] for u in usage),
                 'output_tokens': sum(u['completion_tokens'] for u in usage),
                 'cost_usd': sum(u['cost'] for u in usage), 'call_ids': scope['calls'],
                 'exception': ((result or {}).get('exception_info') or {}).get('exception_type') or
                              ((((result or {}).get('agent_result') or {}).get('metadata') or {}).get('agent_exception') or {}).get('type'),
                 'returncode': proc.returncode, 'time': time.time()}
        if incomplete:
            unknown=[r['id'] for r in incomplete if r['state']=='requires_review']
            value.update(usage_complete=not unknown,uncertain_call_ids=unknown,
                         infrastructure_interrupted=True,
                         transport_failed_calls=len(incomplete))
        cap = self.config['solve']['max_input_plus_output_tokens']
        if cap is not None and value['input_tokens'] + value['output_tokens'] > cap:
            raise RuntimeError('trial token limit violated')
        atomic(cached, value)
        return value
