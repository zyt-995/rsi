"""Original KIRA/Modular solver loops with shared chain-history visibility."""
import importlib.util
import json
import os
from pathlib import Path
import sys

from .common import existing_imports

existing_imports()
from methods.evo_fifty.agents import AuditedLoop, KiraAudited as ExistingKira, ModularAudited as ExistingModular
from methods.evo_agents import normalize_kira_tool_calls


class ChainHistory:
    async def run(self, instruction, environment, context):
        history = os.environ.get("SWE_CHAIN_HISTORY")
        if history:
            records = json.loads(Path(history).read_text())
            if records:
                instruction += ("\n\nPrior completed milestones in this chain (public task/agent observations only):\n"
                                + json.dumps(records, ensure_ascii=False))
        return await super().run(instruction, environment, context)


class KiraAudited(ChainHistory, ExistingKira):
    pass


class ModularAudited(ChainHistory, ExistingModular):
    pass


class CandidateAudited:
    def __new__(cls, *args, **kwargs):
        path = Path(os.environ["EVO_ONLINE_CANDIDATE"])
        for name in list(sys.modules):
            if name == "agents" or name.startswith("agents."):
                del sys.modules[name]
        sys.path.insert(0, str(path.parent.parent))
        spec = importlib.util.spec_from_file_location("swe_evo_candidate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from harbor.agents.terminus_2 import Terminus2
        candidate = module.AgentHarness
        if not isinstance(candidate, type) or not issubclass(candidate, Terminus2):
            raise TypeError("candidate must retain the original Terminus2 interface")
        methods = {}
        if hasattr(candidate, "_parse_tool_calls"):
            def parse(self, calls):
                return candidate._parse_tool_calls(self, normalize_kira_tool_calls(calls))
            methods["_parse_tool_calls"] = parse
        wrapped = type("SWEAuditedCandidate", (ChainHistory, AuditedLoop, candidate), methods)
        return wrapped(*args, **kwargs)
