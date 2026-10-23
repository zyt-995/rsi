"""Same KIRA, with the official ReasoningBank prefix appended to its system context."""
import ast
import copy
import os
from pathlib import Path
from unittest.mock import patch
from swe_chain_evo.agents import KiraAudited
from swe_chain_evo.common import atomic, sha
from .native import memory_prefix


class KiraReasoningBank(KiraAudited):
    async def run(self, instruction, environment, context):
        path = Path(os.environ['WITHIN_CHAIN_NOTE'])
        expected = os.environ['WITHIN_CHAIN_NOTE_SHA256']
        if path.is_symlink() or sha(path) != expected:
            raise RuntimeError('Immutable retrieval snapshot changed')
        text = path.read_text()
        prefix = memory_prefix()
        import litellm
        original = litellm.acompletion
        observed = []
        async def with_memory(*args, **kwargs):
            if text:
                messages = copy.deepcopy(kwargs.get('messages', []))
                system = next((m for m in messages if m.get('role') == 'system'), None)
                if system is None:
                    system = {'role': 'system', 'content': ''}
                    messages.insert(0, system)
                if not isinstance(system.get('content'), str):
                    raise ValueError('Unexpected system-message representation')
                system['content'] += prefix + text
                kwargs['messages'] = messages
            observed.append({'nonempty': bool(text), 'memory_sha256': expected})
            return await original(*args, **kwargs)
        atomic(self.logs_dir / 'reasoning-bank-injection.json', {
            'memory_sha256': expected, 'memory_text': text, 'nonempty': bool(text),
            'injection_location': 'system', 'official_prefix': prefix})
        try:
            with patch.object(litellm, 'acompletion', with_memory):
                return await super().run(instruction, environment, context)
        finally:
            atomic(self.logs_dir / 'reasoning-bank-injection-calls.json', observed)
