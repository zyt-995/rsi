"""Meta-Harness search on the completed Evo prefix, with full file history.

The paper's coding-agent prior and accuracy frontier are retained. Its Claude
backend is replaced by two DeepSeek tool-using coding sessions (analysis and
implementation); each has the whole source/history workspace, not a compressed
single-task prompt. No three-call limit or restricted subclass search is used.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from openai import OpenAI
from methods.evo_trial import PROJECT, sha
from .trial import atomic

VENDOR = PROJECT/'methods/vendor/meta-harness/reference_examples/terminal_bench_2'
BASELINE = VENDOR/'agents/baseline_kira.py'
PRIOR = VENDOR/'.claude/skills/meta-harness-terminal-bench-2/SKILL.md'


def tool(name, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': name + ' in the experiment workspace',
           'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


TOOLS = [
    tool('Read', {'path': {'type': 'string'}, 'offset': {'type': 'integer'}, 'lines': {'type': 'integer'}}, ['path']),
    tool('Glob', {'pattern': {'type': 'string'}}, ['pattern']),
    tool('Grep', {'pattern': {'type': 'string'}, 'glob': {'type': 'string'}}, ['pattern']),
    tool('Write', {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content']),
    tool('Edit', {'path': {'type': 'string'}, 'old': {'type': 'string'}, 'new': {'type': 'string'}}, ['path', 'old', 'new']),
    tool('Bash', {'command': {'type': 'string'}}, ['command']),
    tool('finish', {'summary': {'type': 'string'}}, ['summary']),
]


class Files:
    def __init__(self, root: Path, readonly=False):
        self.root, self.readonly = root.resolve(), readonly

    def path(self, value, write=False):
        path = (self.root/value).resolve()
        if not path.is_relative_to(self.root): raise ValueError('path outside source/history workspace')
        rel = path.relative_to(self.root)
        if write and (self.readonly or not (rel.parts[0] in ('agents', 'prompt-templates') or str(rel) == 'pending_eval.json')):
            raise ValueError('only new candidate, prompt templates and pending_eval.json are writable')
        if write and rel.parts[0] == 'agents' and rel.name in ('baseline_kira.py', 'current_parent.py', '__init__.py'):
            raise ValueError('reference agent is immutable')
        return path

    def call(self, name, args):
        if name == 'Read':
            lines = self.path(args['path']).read_text().splitlines()
            start = max(0, args.get('offset', 0)); count = min(args.get('lines', 250), 1500)
            return '\n'.join(f'{i+1}: {line}' for i,line in enumerate(lines[start:start+count],start))[:50000]
        if name == 'Glob':
            return '\n'.join(str(p.relative_to(self.root)) for p in sorted(self.root.glob(args['pattern'])) if p.is_file())[:40000]
        if name == 'Grep':
            regex = re.compile(args['pattern'])
            rows = []
            for p in sorted(self.root.glob(args.get('glob') or '**/*')):
                if not p.is_file(): continue
                try:
                    for i,line in enumerate(p.read_text().splitlines(),1):
                        if regex.search(line): rows.append(f'{p.relative_to(self.root)}:{i}: {line[:1200]}')
                except (UnicodeError, OSError): pass
                if len(rows) >= 100: break
            return '\n'.join(rows)[:50000]
        if name == 'Write':
            p = self.path(args['path'], True); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(args['content'])
            return 'written ' + str(p.relative_to(self.root))
        if name == 'Edit':
            p = self.path(args['path'], True); text = p.read_text()
            if text.count(args['old']) != 1: raise ValueError('old text must occur exactly once')
            p.write_text(text.replace(args['old'], args['new'], 1)); return 'edited'
        if name == 'Bash':
            # The outer runner owns execution/benchmarking. The coding workspace
            # offers the compile check used by the original implementer prior.
            import shlex
            parts = shlex.split(args['command'])
            if len(parts) < 4 or parts[0] not in ('python', 'python3') or parts[1:3] != ['-m', 'py_compile']:
                raise ValueError('Bash supports python -m py_compile <candidate>; use file tools for inspection/editing')
            files = [self.path(s) for s in parts[3:]]
            env = {k:v for k,v in os.environ.items() if k not in ('OPENROUTER_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','DEEPSEEK_API_KEY')}
            proc = subprocess.run([str(PROJECT/'.venv-terminal/bin/python'), '-m', 'py_compile', *map(str,files)],
                                  cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
            return f'exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}'[-12000:]
        if name == 'finish': return args['summary']
        raise ValueError('unknown tool')


def coding_session(runner, workspace, output, phase, instruction, readonly=False):
    output.mkdir(parents=True, exist_ok=True)
    if (output/'completed.json').exists(): return json.loads((output/'completed.json').read_text())['summary']
    if list(output.glob('response-*.json')): raise RuntimeError('partial coding session; preserve usage rather than replay')
    token = runner.broker.scope('meta-harness', phase, str(output.relative_to(runner.root)), limit=10**9)
    client = OpenAI(base_url=f'http://127.0.0.1:{runner.port}/v1', api_key=token, max_retries=0, timeout=2700)
    prior = PRIOR.read_text().replace('Claude Opus 4.6', 'DeepSeek V4.1 Flash')
    messages = [{'role':'system', 'content': prior + '\nOnline Evo adaptation: use only the completed-task history in this workspace. The task model, gateway, budget, grader and runner are fixed. No task answers or task names in reusable agent code. The outer runner evaluates candidates. Your role is '+phase+'.'},
                {'role':'user', 'content':instruction}]
    files = Files(workspace, readonly)
    summary = ''
    for turn in range(200):
        result = client.chat.completions.create(model=runner.config['model']['id'], messages=messages,
                    tools=[t for t in TOOLS if not readonly or t['function']['name'] in ('Read','Glob','Grep','finish')],
                    tool_choice='auto', max_tokens=32768)
        atomic(output/f'response-{turn:03d}.json', result.model_dump())
        message = result.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            summary = message.content or ''
            if result.choices[0].finish_reason == 'length':
                messages.append({'role':'user','content':'Continue the analysis or file edit; the previous output reached its limit.'})
                continue
            break
        finished = False
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments)
                answer = files.call(call.function.name, args)
            except Exception as exc: answer = type(exc).__name__ + ': ' + str(exc)
            messages.append({'role':'tool','tool_call_id':call.id,'content':answer})
            if call.function.name == 'finish': summary, finished = answer, True
        atomic(output/'messages.json', messages)
        if finished: break
    atomic(output/'completed.json', {'summary':summary,'turns':turn+1,'time':time.time()})
    return summary


def copy_public_history(runner, rows, workspace):
    for row in rows:
        dest = workspace/'history'/f'{row["index"]:06d}'
        dest.mkdir(parents=True, exist_ok=True)
        trial = Path(row['result']).parent
        if not (dest/'agent').exists(): shutil.copytree(trial/'agent', dest/'agent')
        atomic(dest/'score.json', {k:row[k] for k in ('task','reward','input_tokens','output_tokens','cost_usd')})
        shutil.copy2(Path(runner.stream[row['index']]['path'])/'instruction.md', dest/'instruction.md')


def update(runner, index, parent, rows, output):
    output = Path(output); decision = output/'decision.json'
    if decision.exists(): return json.loads(decision.read_text())
    workspace = runner.root/'meta-workspace'
    (workspace/'agents').mkdir(parents=True, exist_ok=True)
    if not (workspace/'agents/baseline_kira.py').exists():
        shutil.copy2(BASELINE, workspace/'agents/baseline_kira.py')
        (workspace/'agents/__init__.py').write_text('')
        shutil.copytree(VENDOR/'prompt-templates', workspace/'prompt-templates')
    shutil.copy2(parent, workspace/'agents/current_parent.py')
    copy_public_history(runner, rows, workspace)
    summary_path = workspace/'evolution_summary.jsonl'
    summary_path.touch(exist_ok=True)
    name = f'candidate_{index:06d}'
    analysis = coding_session(runner, workspace, output/'analysis', 'analysis',
        'Read agents/baseline_kira.py, agents/current_parent.py, evolution_summary.jsonl and the successful and failed raw history trajectories. Form one reusable harness improvement hypothesis. Report the mechanism, prediction and implementation plan. Use file tools to inspect the complete relevant source and evidence.', True)
    coding_session(runner, workspace, output/'implementation', 'editing',
        f'Implement this single hypothesis: {analysis}\nCreate agents/{name}.py containing the full AgentHarness class, subclassing Terminus2. Copy a parent as your starting point; you may override any harness method and create a prompt template. Use the compile check. Then write pending_eval.json with one candidate named {name}, its hypothesis and changes. Submit the files; do not run benchmarks.')
    candidate = workspace/'agents'/f'{name}.py'
    if not candidate.exists():
        value = {'promoted':False,'reason':'coding_agent_submitted_no_candidate','candidate':None}
    else:
        try:
            ast.parse(candidate.read_text()); compile(candidate.read_text(), str(candidate), 'exec')
            if any(row['task'] in candidate.read_text() for row in rows): raise ValueError('task-specific reference in candidate')
        except Exception as exc:
            value = {'promoted':False,'reason':'candidate_validation_failed','error':str(exc),'candidate':str(candidate)}
        else:
            cases=[]
            # Re-score both harnesses on exactly the same visible prefix because
            # this evaluation pool grows online. Historical averages from a
            # smaller prefix must not be used as the incumbent comparison.
            for row in rows:
                for attempt in range(2):
                    ref = runner.run('meta-harness', row['index'], output/'parent-validation'/f'{row["index"]:06d}-{attempt}',
                                     phase='validation', candidate=parent)
                    new = runner.run('meta-harness', row['index'], output/'candidate-validation'/f'{row["index"]:06d}-{attempt}',
                                     phase='validation', candidate=candidate)
                    cases.append({'index':row['index'],'attempt':attempt,'parent_reward':ref['reward'],'candidate_reward':new['reward']})
            before = sum(c['parent_reward'] for c in cases)/len(cases)
            after = sum(c['candidate_reward'] for c in cases)/len(cases)
            promoted = after > before
            value={'promoted':promoted,'reason':'accuracy_frontier_improved' if promoted else 'accuracy_frontier_not_improved',
                   'candidate':str(candidate),'candidate_sha256':sha(candidate),'parent_accuracy':before,
                   'candidate_accuracy':after,'validation_cases':cases}
    atomic(decision,value)
    with summary_path.open('a') as stream: stream.write(json.dumps({'iteration':index+1,**value})+'\n')
    # Keep source files, scores and traces in the coding agent's persistent view.
    public = workspace/'search_history'/f'{index:06d}'
    public.mkdir(parents=True,exist_ok=True)
    atomic(public/'decision.json',value)
    for folder in ('parent-validation','candidate-validation'):
        if (output/folder).exists():
            for p in (output/folder).glob('*/summary.json'):
                s=json.loads(p.read_text()); dest=public/folder/p.parent.name
                dest.mkdir(parents=True,exist_ok=True)
                atomic(dest/'score.json',{k:s[k] for k in ('task','reward','input_tokens','output_tokens','cost_usd')})
                if not (dest/'agent').exists(): shutil.copytree(Path(s['result']).parent/'agent',dest/'agent')
    return value
