"""Run the unmodified official Meta-Harness loop with Evo evaluation seams.

No replacement proposer or frontier implementation is used. The original
Claude Code wrapper and its full tool set run in a workspace sandbox through
the audited model gateway. Only the dataset, model transport and launcher are
adapted. Historical candidates are rescored when the visible Evo pool grows.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import ExitStack
from unittest.mock import patch

from methods.evo_trial import PROJECT, sha
from .trial import atomic

VENDOR = PROJECT/'methods/vendor/meta-harness/reference_examples/terminal_bench_2'


def isolated_command(command, workspace):
    binary = Path(shutil.which('claude')).resolve()
    args = ['bwrap', '--die-with-parent', '--unshare-user', '--unshare-pid',
            '--ro-bind', '/usr', '/usr', '--ro-bind', '/bin', '/bin',
            '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
            '--ro-bind', '/etc/ssl', '/etc/ssl', '--ro-bind', '/etc/resolv.conf', '/etc/resolv.conf',
            '--ro-bind', '/etc/hosts', '/etc/hosts', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--dir', '/opt', '--ro-bind', str(binary), '/opt/claude',
            '--bind', str(workspace), str(workspace), '--chdir', str(workspace)]
    if '--plugin-dir' in command:
        args += ['--dir', command[command.index('--plugin-dir')+1]]
    import harbor
    # Expose the pinned Python runtime and Harbor source, without exposing the
    # surrounding benchmark task directories or the coordinator's secrets.
    for p in (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(harbor.__file__).parent.resolve()):
        args += ['--ro-bind', str(p), str(p)]
    # The venv's interpreter symlink retains this absolute launcher alias.
    # Bind it to the identical pinned runtime inside the workspace sandbox.
    interpreter=Path(sys.prefix).resolve()/'bin/python3'
    if interpreter.is_symlink():
        alias=interpreter.readlink()
        if alias.is_absolute():
            args += ['--ro-bind',str(Path(sys.base_prefix).resolve()),str(alias.parent.parent)]
    uv=Path(shutil.which('uv') or sys.executable).resolve().parent
    if uv.is_dir(): args += ['--ro-bind',str(uv),str(uv)]
    for folder in ('evo-history', 'jobs'):
        p=workspace/folder
        if p.exists(): args += ['--ro-bind', str(p), str(p)]
    for p in list(workspace.glob('*.py'))+list((workspace/'agents').glob('*.py')):
        args += ['--ro-bind', str(p), str(p)]
    for p in (workspace/'logs').glob('**/frontier_val.json'):
        args += ['--ro-bind', str(p), str(p)]
    for p in (workspace/'logs').glob('**/evolution_summary.jsonl'):
        args += ['--ro-bind', str(p), str(p)]
    return args + ['/opt/claude', *command[1:]]


def public_transport(runner, summary, destination):
    for call_id in summary.get('call_ids', []):
        target=destination/call_id;target.mkdir(parents=True,exist_ok=True)
        source=runner.root/'provider-calls'/call_id
        for name in ('request.json','response.json'):
            if (source/name).exists():shutil.copy2(source/name,target/name)


def load_native(workspace):
    # This directory has only native method code, candidates and visible-task
    # history. No task tree, grader, credential file or future stream is bound.
    if not workspace.exists():
        shutil.copytree(VENDOR, workspace, ignore=shutil.ignore_patterns('jobs', 'logs', '__pycache__', '*.pyc'))
    sys.path.insert(0, str(workspace))
    spec = importlib.util.spec_from_file_location('evo_native_meta_harness', workspace/'meta_harness.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_metadata(native, job, rows, n_trials):
    """Let the official cache guard reject an incomplete Evo job."""
    job = Path(job)
    job.mkdir(parents=True, exist_ok=True)
    expected = {}
    for row in rows:
        expected[row['task']] = expected.get(row['task'], 0) + n_trials
    rewards = native.parse_job_results(job)
    complete = {task: len(values) for task, values in rewards.items()} == expected
    atomic(job/'config.json', {'model': native.MODEL,
           'n_attempts': n_trials if complete else 0,
           'evo_task_indices': [row['index'] for row in rows],
           'evo_cache_complete': complete})
    return complete


def evaluation_source(workspace, job, candidate):
    """Freeze exact evaluation inputs; reconstruct old trials only by hash."""
    from methods.modular_update import tree_hashes
    frozen = Path(job) / 'evo-evaluation-source'
    provenance = frozen / 'provenance.json'
    if provenance.exists():
        expected = json.loads(provenance.read_text())
    else:
        summaries = sorted((Path(job) / 'evo-trials').glob('*/summary.json'))
        fingerprints = [json.loads(p.read_text()).get('source_fingerprint') for p in summaries]
        fingerprints = [p for p in fingerprints if p]
        if fingerprints:
            expected = {k: fingerprints[0][k] for k in ('agents_source', 'prompt_templates')}
            if any(any(p[k] != expected[k] for k in expected) for p in fingerprints):
                raise RuntimeError('evaluation job mixed different source trees')
        else:
            expected = {'agents_source': tree_hashes(workspace / 'agents'),
                        'prompt_templates': tree_hashes(workspace / 'prompt-templates')}
        for key, folder in [('agents_source', 'agents'), ('prompt_templates', 'prompt-templates')]:
            for relative, digest in expected[key].items():
                source = workspace / folder / relative
                if sha(source) != digest:
                    raise RuntimeError('evaluation input changed: ' + str(source))
                target = frozen / folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        atomic(provenance, expected)
    for key, folder in [('agents_source', 'agents'), ('prompt_templates', 'prompt-templates')]:
        if tree_hashes(frozen / folder) != expected[key]:
            raise RuntimeError('frozen evaluation input changed')
    source = frozen / 'agents' / Path(candidate).name
    if sha(source) != sha(Path(candidate)):
        raise RuntimeError('candidate changed since evaluation started')
    return source


def update(runner, index, parent, rows, output):
    cfg = runner.config['arms']['meta-harness']
    trials = cfg.get('candidate_validation_attempts_per_task', 2)
    rows = rows[-cfg.get('validation_task_limit', len(rows)):]
    output = Path(output)
    if (output/'decision.json').exists():
        return json.loads((output/'decision.json').read_text())
    output.mkdir(parents=True, exist_ok=True)
    workspace = runner.root/'native-meta-workspace'
    native = load_native(workspace)
    assert sha(workspace/'meta_harness.py') == sha(VENDOR/'meta_harness.py')
    history = workspace/'evo-history'
    for row in rows:
        dest = history/f'{row["index"]:06d}'
        dest.mkdir(parents=True, exist_ok=True)
        if not (dest/'agent').exists():
            shutil.copytree(Path(row['result']).parent/'agent', dest/'agent')
        shutil.copy2(Path(runner.stream[row['index']]['path'])/'instruction.md', dest/'instruction.md')
        atomic(dest/'score.json', {k:row[k] for k in ('task', 'reward', 'input_tokens', 'output_tokens')})
        public_transport(runner,row,dest/'provider-trace')

    run_name = f'evo_{index:06d}'
    native.MODEL = 'openai/' + runner.config['model']['id']
    native.N_EVAL_TASKS = len(rows)
    native.SMOKE_TEST_TASK = rows[-1]['task']
    native.LOGS_DIR = workspace/'logs'/run_name
    native.JOBS_DIR = workspace/'jobs'/run_name
    native.FRONTIER_VAL = native.LOGS_DIR/'frontier_val.json'
    native.EVOLUTION_SUMMARY = native.LOGS_DIR/'evolution_summary.jsonl'
    native.PENDING_EVAL = native.LOGS_DIR/'pending_eval.json'
    native.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    native.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    original_prompt, original_cmd = native.render_task_prompt, native.run_cmd
    original_build = native.claude_wrapper.build_command

    def candidate_path(import_path):
        module, cls = import_path.rsplit(':', 1)
        if cls != 'AgentHarness' or not module.startswith('agents.') or '/' in module or '..' in module:
            raise ValueError('candidate must be an official agents module')
        path = workspace.joinpath(*module.split('.')).with_suffix('.py').resolve()
        if not path.is_relative_to(workspace/'agents') or not path.is_file():
            raise ValueError('candidate outside official agents workspace')
        return path

    def evaluate(import_path, job_name, n_trials=trials, n_concurrent=None, selected=None):
        job = native.JOBS_DIR/job_name
        job.mkdir(parents=True, exist_ok=True)
        source = evaluation_source(workspace, job, candidate_path(import_path))
        def attempt_trial(row, attempt):
                summary = runner.run('meta-harness', row['index'],
                    job/'evo-trials'/f'{row["index"]:06d}-{attempt}', phase='validation', candidate=source)
                trial = Path(summary['result']).parent
                public = job/trial.name
                public.mkdir(exist_ok=True)
                shutil.copy2(trial/'result.json', public/'result.json')
                if not (public/'agent').exists(): shutil.copytree(trial/'agent', public/'agent')
                public_transport(runner,summary,public/'agent/provider-trace')
                if 'input_tokens' in summary:
                    raw=json.loads((trial/'result.json').read_text())
                    atomic(public/'raw-result.json',raw)
                    ar={**(raw.get('agent_result') or {}),'n_input_tokens':summary['input_tokens'],
                        'n_output_tokens':summary['output_tokens'],'cost_usd':summary['cost_usd']}
                    atomic(public/'result.json',{**raw,'agent_result':ar})
                    atomic(public/'accounting-provenance.json',{'original_result_sha256':sha(trial/'result.json'),
                        'call_ids':summary['call_ids'],'reward_unchanged':True,'source':'authoritative gateway receipts'})
                return bool(summary['exception'])
        concurrency = n_concurrent or runner.config['runtime'].get('validation_concurrency', 1)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures=[pool.submit(attempt_trial,row,attempt) for row in selected or rows for attempt in range(n_trials)]
            errors=sum(future.result() for future in futures)
        atomic(job/'result.json', {'stats': {'n_errors': errors}})
        if not cache_metadata(native, job, selected or rows, n_trials):
            raise RuntimeError('Evo evaluation job does not contain its complete task/trial set')
        return job, True

    def run_cmd(command, timeout=native.HARBOR_TIMEOUT_SECONDS, cwd=None):
        if (completed_proposal is not None and command[:2] == ['rm', '-rf'] and
            Path(command[2]).parent == native.JOBS_DIR and Path(command[2]).name.startswith('smoke-')):
            # Preserve the finished smoke trial; evaluate() checks and reuses
            # its exact source and immutable result through TrialRunner.
            return subprocess.CompletedProcess(command, 0, '', '')
        if str(command[0]).endswith('scripts/run_eval.sh'):
            job_name = command[command.index('--job-name')+1]
            job, _ = evaluate(command[1], job_name, 1, 1, [rows[-1]])
            return subprocess.CompletedProcess(command, 0, '', '')
        if command[:3] == ['uv', 'run', 'python']:
            command = [str(PROJECT/'.venv-terminal/bin/python'), *command[3:]]
        return original_cmd(command, timeout=timeout, cwd=cwd)

    def render(iteration, n_trials):
        return original_prompt(iteration, n_trials).replace('official TB2 tasks', 'completed Evo tasks').replace(
            'full TB2 dataset from the paper runs', 'completed Evo task prefix').replace('(Opus)', '(DeepSeek)') + (
            '\nThis is an online Evo run. The attached skill describes the original dataset/model; '
            'use the current DeepSeek model and only evo-history/ and jobs/ for completed tasks. '
            'Use all original search tools, source files and historical candidates. Submit candidate(s) '
            'to the pending_eval.json path above. Benchmark execution belongs to the outer Evo adapter. '
            'Do not access future tasks or encode task-specific answers. The global spend guard may stop the run.\n') + (
            '\nResume the interrupted native session. Reuse its completed analysis and existing edits; '
            'finish this iteration and submit pending_eval.json.\n' if resume_session else '')

    def build(*args, **kwargs):
        command=original_build(*args, **kwargs)
        if resume_session: command += ['--resume', resume_session]
        return isolated_command(command, workspace)

    scope_name=str(output.relative_to(runner.root))+'/native-claude-code'
    resume_session=None
    completed_proposal=None
    previous=sorted([(t,s) for t,s in runner.broker.scopes.items()
                     if s['name']==scope_name or s['name'].startswith(scope_name+'-resume-')],
                    key=lambda pair:pair[1]['name'])
    if previous:
        token,s=previous[-1]
        if s['arm']!='meta-harness' or s['phase']!='proposal':
            raise RuntimeError('invalid native proposer checkpoint')
        if s['calls'] or s['pending_tokens'] or s['blocked']:
            sessions=sorted(native.LOGS_DIR.glob('claude_sessions/*/meta.json'))
            meta=json.loads(sessions[-1].read_text()) if sessions else {}
            if (meta.get('exit_code')==0 and native.PENDING_EVAL.exists() and
                not s['pending_tokens'] and not s['blocked'] and
                not (native.EVOLUTION_SUMMARY.exists() and native.EVOLUTION_SUMMARY.read_text().strip())):
                completed_proposal=json.loads(native.PENDING_EVAL.read_text())
                for c in completed_proposal.get('candidates', []):
                    candidate_path(c['import_path'])
                if not completed_proposal.get('candidates'):
                    raise RuntimeError('completed proposer has no recorded candidates')
                atomic(output/'completed-proposer-recovery.json', {
                    'session_id':meta.get('session_id'), 'paid_proposer_replayed':False,
                    'pending_eval_sha256':sha(native.PENDING_EVAL),
                    'candidate_hashes':{c['name']:sha(candidate_path(c['import_path']))
                                        for c in completed_proposal['candidates']}})
            elif (not meta.get('session_id') or meta.get('exit_code')==0 or native.PENDING_EVAL.exists()
                or (native.EVOLUTION_SUMMARY.exists() and native.EVOLUTION_SUMMARY.read_text().strip())):
                raise RuntimeError('partial paid native proposer; no safe session continuation')
            if completed_proposal is None:
                resume_session=meta['session_id']
                saved=list((workspace/'.claude-private/projects').glob('*/'+resume_session+'.jsonl'))
                if len(saved)!=1: raise RuntimeError('native resume transcript missing')
                name=scope_name+f'-resume-{len(previous):03d}'
                token=runner.broker.scope('meta-harness','proposal',name,limit=10**9)
                atomic(output/f'native-resume-{len(previous):03d}.json',
                       {'session_id':resume_session,'previous_scope':s['name'],
                        'previous_call_ids':s['calls'],'saved_transcript_sha256':sha(saved[0]),
                        'formal_trials_replayed':False,'continuation_scope':name})
    else:
        token = runner.broker.scope('meta-harness', 'proposal', scope_name, limit=10**9)
    model = runner.config['model']['id']
    env = {k:v for k,v in os.environ.items() if k not in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY',
           'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDECODE', 'DEEPSEEK_API_KEY')}
    import harbor
    env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{runner.port}', ANTHROPIC_AUTH_TOKEN=token,
               ANTHROPIC_API_KEY='', CLAUDE_CONFIG_DIR=str(workspace/'.claude-private'),
               CLAUDE_CODE_SUBAGENT_MODEL=model, ANTHROPIC_MODEL=model,
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_AUTO_UPDATE='1',
               HTTP_PROXY=runner.config['runtime']['host_outbound_proxy'], HTTPS_PROXY=runner.config['runtime']['host_outbound_proxy'],
               NO_PROXY='127.0.0.1,localhost', PYTHONPATH=os.pathsep.join([str(workspace),str(Path(harbor.__file__).parent.parent.resolve())]),
               PATH=os.pathsep.join([str(Path(sys.prefix).resolve()/'bin'),str(Path(shutil.which('uv') or sys.executable).resolve().parent),env.get('PATH','')]),PYTHONDONTWRITEBYTECODE='1')
    for role in ('OPUS', 'SONNET', 'HAIKU', 'FABLE'):
        env['ANTHROPIC_DEFAULT_'+role+'_MODEL'] = model
    atomic(output/'native-entry.json', {'entry': str(workspace/'meta_harness.py'),
           'source_sha256': sha(VENDOR/'meta_harness.py'), 'task_prefix': [r['task'] for r in rows],
           'proposer': 'official Claude Code wrapper and tool set; isolated workspace',
           'frontier': 'official update_frontier; historical candidates rescored on same visible prefix',
           'trials': trials, 'official_algorithms_modified': False, 'workload_profile': cfg.get('workload_profile', 'train')})
    # A controller restart can leave only part of a baseline's public job.
    # Require the full prefix before the unmodified upstream loop trusts it;
    # evaluate() reconstructs public copies from private trial checkpoints.
    for name, _ in native.BASELINES:
        cache_metadata(native, native.JOBS_DIR/(name+f'-t{trials}'), rows, trials)
    # No paid proposer has started (guarded above). Rebuild the current-prefix
    # frontier from complete evaluations, preserving any interrupted snapshot.
    if native.FRONTIER_VAL.exists():
        recovery = output/'frontier-before-cache-recovery.json'
        if not recovery.exists(): shutil.copy2(native.FRONTIER_VAL, recovery)
        native.FRONTIER_VAL.unlink()
    # A growing Evo pool needs all incumbent scores to use the same tasks.
    # The official frontier implementation itself is called unchanged.
    with patch.dict(os.environ, env, clear=True), patch.object(native, 'harbor_run', evaluate), \
         patch.object(native, 'run_cmd', run_cmd), patch.object(native, 'render_task_prompt', render), \
         patch.object(native.claude_wrapper, 'build_command', build):
        with ExitStack() as recovery:
            if completed_proposal is not None:
                def restore_proposal(*args, **kwargs):
                    atomic(native.PENDING_EVAL, completed_proposal)
                    return True
                recovery.enter_context(patch.object(native, 'propose_claude', restore_proposal))
            pending_names = {c['name'] for c in completed_proposal['candidates']} if completed_proposal else set()
            _run_search(native, workspace, evaluate, runner, pending_names)

    state = runner.broker.scopes[token]
    if state['blocked'] or state['pending_tokens'] or any(s=='over_bound' for _,s in runner.broker.ledger.snapshot()['blocking']):
        raise RuntimeError('native proposer provider accounting requires review')
    frontier = json.loads(native.FRONTIER_VAL.read_text())
    best = frontier['_best']['agent']
    if best == 'kira-baseline':
        winner = workspace/'agents/baseline_kira.py'; job_name=f'kira-baseline-t{trials}'
    elif best == 'terminus2-baseline':
        winner = workspace/'agents/baseline_terminus2.py'; job_name=f'terminus2-baseline-t{trials}'
    else:
        winner = workspace/'agents'/f'{best}.py'
        job_name = ('evolve-'+best+f'-t{trials}') if (native.JOBS_DIR/('evolve-'+best+f'-t{trials}')).exists() else 'historical-'+best
    if not winner.is_file(): raise RuntimeError('official frontier selected an absent candidate')
    winner = evaluation_source(workspace, native.JOBS_DIR/job_name, winner)
    promoted = sha(winner) != sha(Path(parent))
    deployment = output/'deployed'
    if not deployment.exists():
        shutil.copytree(winner.parent, deployment/'agents', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(winner.parent.parent/'prompt-templates', deployment/'prompt-templates')
    selected = deployment/'agents'/winner.name
    decision = {'promoted': promoted, 'candidate': str(selected), 'candidate_sha256': sha(selected),
                'reason': 'official_frontier_selection', 'frontier': frontier, 'native_logs': str(native.LOGS_DIR)}
    atomic(output/'decision.json', decision)
    return decision


def _run_search(native, workspace, evaluate, runner, pending_names):
    trials = runner.config['arms']['meta-harness'].get('candidate_validation_attempts_per_task', 2)
    history_candidates = sorted((workspace/'agents').glob('*.py'))
    for p in history_candidates:
        if p.name in ('__init__.py', 'baseline_kira.py', 'baseline_terminus2.py'): continue
        if p.stem in pending_names: continue
        imp = f'agents.{p.stem}:AgentHarness'
        if native.validate_candidate(p.stem, imp):
            job, _ = evaluate(imp, 'historical-'+p.stem)
            rates, avg = native.compute_pass_rates(native.parse_job_results(job, expected_trials=trials))
            native.update_frontier({p.stem: (rates, avg)})
    native.run_evolve(argparse.Namespace(run_name=native.LOGS_DIR.name, fresh=False, skip_baseline=False,
         iterations=1, trials=trials, concurrent=runner.config['runtime'].get('validation_concurrency',1),
         propose_timeout=2400, skip_smoke=False, full_eval=False))
