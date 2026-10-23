"""Execute original function ASTs verbatim, replacing external SDK I/O bindings only.

The Google project root requires Python 3.13 and bundles a different solver. Our
common solver is pinned to Python 3.12.14. Loading the selected definitions avoids
importing its unused BrowserGym, mini-SWE and Google credential initializers.
Every function retains its original source filename and AST; torch is real CPU
PyTorch. No retrieval, judge, extraction, or selection body is rewritten.
"""
import ast
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import contextlib
import io

from . import VENDOR

MEMORY = VENDOR / 'third_party/src/minisweagent/memory/memory_management.py'
SWE = VENDOR / 'third_party/src/minisweagent/run/extra/swebench.py'
PROMPTS = VENDOR / 'third_party/src/minisweagent/memory/instruction.py'


def definitions(path, names, bindings):
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in nodes} != set(names):
        raise RuntimeError('Pinned upstream entrypoints changed')
    node = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    ast.fix_missing_locations(node)
    namespace = dict(bindings)
    exec(compile(node, str(path), 'exec'), namespace)
    return namespace


def prompt_values():
    namespace = {}
    exec(compile(PROMPTS.read_text(), str(PROMPTS), 'exec'), namespace)
    return namespace['SUCCESSFUL_SI'], namespace['FAILED_SI']


def memory_prefix():
    path = VENDOR / 'third_party/src/minisweagent/agents/default.py'
    matches = [n.value for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and n.value.startswith('\n\nBelow are some memory items')]
    if len(matches) != 1:
        raise RuntimeError('Official memory system instruction changed')
    return matches[0]


def source_evidence():
    result = {}
    for path in (MEMORY, SWE, PROMPTS, VENDOR / 'third_party/src/minisweagent/agents/default.py'):
        result[str(path.relative_to(VENDOR))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def retrieval(bank, query, task_id, cache_path, embed):
    import torch
    class TextEmbeddingInput:
        def __init__(self, text, task_type):
            self.text, self.task_type = text, task_type
    class TextEmbeddingModel:
        @staticmethod
        def from_pretrained(model):
            if model != 'gemini-embedding-001':
                raise ValueError('Embedding model changed')
            class Model:
                def get_embeddings(self, values, output_dimensionality):
                    return [SimpleNamespace(values=embed(v.text, v.task_type, output_dimensionality)) for v in values]
            return Model()
    namespace = definitions(MEMORY, ['l2_normalize', 'embed_query_with_gemini', 'load_cached_embeddings',
        'get_detailed_instruct', 'select_memory', 'screening'],
        {'torch': torch, 'F': torch.nn.functional, 'os': __import__('os'), 'json': json,
         'logger': logging.getLogger('reasoning_bank.native'), 'TextEmbeddingModel': TextEmbeddingModel,
         'TextEmbeddingInput': TextEmbeddingInput})
    selected = namespace['select_memory'](1, reasoning_bank=bank, cur_query=query,
                     task_id=task_id, cache_path=str(cache_path), prefer_model='gemini')
    # Exact memory assembly in upstream process_instance().
    text = '\n\n'.join(item for experience in selected for item in experience['memory_items']) if selected else ''
    return selected, text


def judge_and_extract(query, trajectory, model, generate, phase_checkpoint=None):
    def content(model, contents, config):
        role = 'judge' if config.temperature == 0.0 else 'extraction'
        return SimpleNamespace(text=generate(role, model, contents, config))
    namespace = definitions(SWE, ['llm_generate', 'llm_judge_status'], {
        'client': SimpleNamespace(models=SimpleNamespace(generate_content=content)),
        'GenerateContentConfig': lambda **kwargs: SimpleNamespace(**kwargs)})
    status = namespace['llm_judge_status'](query, trajectory, model)
    success, failed = prompt_values()
    value = f'**Query:** {query}\n\n**Trajectory:**\n{trajectory}'
    # verbose=False changes printing only; original function/prompt/parser are used.
    items = namespace['llm_generate'](value, model, False, si=success if status else failed)
    return status, items


def public_trajectory(path):
    """Map Harbor ATIF to the non-system message content consumed by mini-SWE."""
    value = json.loads(Path(path).read_text())
    if 'steps' not in value:
        raise ValueError('Expected a public ATIF agent trajectory')
    messages = []
    for step in value['steps']:
        if step.get('source') == 'system':
            continue
        # Keep public action/observation/thinking; exclude timing, usage and private grader data.
        public = {k: step[k] for k in ('source', 'message', 'reasoning_content', 'tool_calls', 'observation')
                  if step.get(k) is not None}
        messages.append(json.dumps(public, ensure_ascii=False))
    if not messages:
        raise ValueError('No public execution trace was recorded')
    return '\n'.join(messages)
