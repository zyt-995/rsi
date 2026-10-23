"""Make checkout-local modules importable before editable installation."""
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
for path in (REPO / 'src', REPO / 'src/runner', REPO / 'src/benchmark', REPO / 'src/agents'):
    sys.path.insert(0, str(path))
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
