"""Pinned official ReasoningBank functions with a comparable KIRA/SWE execution seam."""
import os
from pathlib import Path
import sys
from rsi_portable.paths import REPO

VENDOR = REPO / 'upstream/reasoning-bank'
COMMIT = 'ed80611788292ea739f1effd31f16c53823b8a0d'
extra = os.environ.get('RSI_RB_DEPS')
if extra and extra not in sys.path:
    sys.path.append(extra)
