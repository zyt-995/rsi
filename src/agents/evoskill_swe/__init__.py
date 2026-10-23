"""Chronological benchmark adapter around pinned, unmodified EvoSkill operators."""
import os
from pathlib import Path
import sys

from rsi_portable.paths import WORKSPACE as RELEASE
VENDOR = RELEASE / "upstream/EvoSkill"
BACKBONE = RELEASE
import site
SDK_SITE = Path(site.getsitepackages()[0])
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
for path in (BACKBONE, BACKBONE / "runtime-project", VENDOR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
# Use the already installed official SDK; never upgrade the active experiment's venv.
if str(SDK_SITE) not in sys.path:
    sys.path.append(str(SDK_SITE))
