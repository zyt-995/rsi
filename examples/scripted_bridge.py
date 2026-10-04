"""Executable JSON bridge example; use an absolute path in --agent-command.

Replace ScriptedAgent.decide with your model call, construct a prompt from ONLY
the request, and return the provider's actual input/output token usage.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsi_harness.agents import ScriptedAgent


if __name__ == "__main__":
    request = json.load(sys.stdin)
    json.dump(ScriptedAgent().decide(request), sys.stdout)
    sys.stdout.write("\n")
