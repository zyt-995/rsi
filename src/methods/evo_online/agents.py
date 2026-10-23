"""Official solver loops with the common, paid and audited model endpoint."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

from methods.evo_agents import AgentHarness as KiraAgent, Terminus2Modular, normalize_kira_tool_calls
from harbor.llms.base import OutputLengthExceededError


class AdmissionStop(BaseException):
    pass


class AuditedLoop:
    async def run(self, instruction, environment, context):
        import litellm
        self._online_calls = []
        original = litellm.acompletion
        def save():
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            p = self.logs_dir / 'provider-calls.json'
            temp = p.with_suffix('.tmp')
            temp.write_text(json.dumps(self._online_calls, indent=2) + '\n')
            os.replace(temp, p)
        async def observed(*args, **kwargs):
            kwargs.update(model='openai/deepseek/deepseek-v4.1-flash',
                          api_base=os.environ['EVO_ONLINE_API_BASE'],
                          api_key=os.environ['OPENAI_API_KEY'])
            kwargs['max_tokens'] = min(kwargs.get('max_tokens') or 32768, 32768)
            kwargs['timeout'] = 2700
            row = {'request': len(self._online_calls) + 1, 'state': 'started'}
            self._online_calls.append(row)
            save()
            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                row.update(state='error', exception=type(exc).__name__)
                save()
                if getattr(exc, 'status_code', None) == 402:
                    row.update(state='budget_stop', message=str(exc))
                    save()
                    raise AdmissionStop(str(exc))
                raise
            raw = result.model_dump()
            p = self.logs_dir / f'provider-response-{row["request"]:04d}.json'
            p.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str) + '\n')
            usage = raw.get('usage') or {}
            if type(usage.get('prompt_tokens')) is not int or type(usage.get('completion_tokens')) is not int:
                row.update(state='usage_missing')
                save()
                raise RuntimeError('provider_usage_missing')
            row.update(state='response', input_tokens=usage['prompt_tokens'],
                       output_tokens=usage['completion_tokens'], generation_id=raw.get('id'))
            save()
            return result
        stop = None
        agent_error = None
        try:
            with patch.object(litellm, 'acompletion', observed):
                await super().run(instruction, environment, context)
        except (AdmissionStop, OutputLengthExceededError) as exc:
            stop = type(exc).__name__ + ': ' + str(exc)
        except Exception as exc:
            # Preserve a method failure and let the common verifier grade the
            # retained filesystem. Broker independently stops uncertain API
            # requests; those trials are excluded by TrialRunner.
            agent_error = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
            (self.logs_dir / 'agent-exception.json').write_text(json.dumps(agent_error, indent=2) + '\n')
        finally:
            rows = [row for row in self._online_calls if row['state'] == 'response']
            context.n_input_tokens = sum(row['input_tokens'] for row in rows)
            context.n_output_tokens = sum(row['output_tokens'] for row in rows)
            context.metadata = {**(context.metadata or {}), 'online_provider_accounting': True,
                                'provider_requests': len(self._online_calls), 'provider_responses': len(rows),
                                'budget_stop_reason': stop, 'agent_exception': agent_error}
            save()


class KiraAudited(AuditedLoop, KiraAgent):
    def _parse_tool_calls(self, calls):
        return super()._parse_tool_calls(normalize_kira_tool_calls(calls))


class ModularAudited(AuditedLoop, Terminus2Modular):
    pass


class CandidateAudited(KiraAudited):
    def __new__(cls, *args, **kwargs):
        path = Path(os.environ['EVO_ONLINE_CANDIDATE'])
        # Generated agents may import their own archived helper/parent modules.
        # Select that complete deployment package rather than the vendor's
        # initial agents package imported by the common adapter above.
        for name in list(sys.modules):
            if name == 'agents' or name.startswith('agents.'):
                del sys.modules[name]
        sys.path.insert(0, str(path.parent.parent))
        spec = importlib.util.spec_from_file_location('evo_online_candidate', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from harbor.agents.terminus_2 import Terminus2
        candidate = module.AgentHarness
        if not isinstance(candidate, type) or not issubclass(candidate, Terminus2):
            raise TypeError('candidate must subclass Terminus2')
        methods = {}
        if hasattr(candidate, '_parse_tool_calls'):
            def parse(self, calls):
                return candidate._parse_tool_calls(self, normalize_kira_tool_calls(calls))
            methods['_parse_tool_calls'] = parse
        wrapped = type('AuditedCandidate', (AuditedLoop, candidate), methods)
        return wrapped(*args, **kwargs)
