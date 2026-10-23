"""Portable fake-provider fixtures; no historical run files or credentials."""
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
for folder in ('src', 'src/runner', 'src/benchmark', 'src/agents'):
    sys.path.insert(0, str(REPO / folder))

def configuration(arm):
    cfg = json.loads((REPO / 'configs/templates/control.json').read_text())
    cfg.update(json.loads((REPO / 'configs/gpt/model.json').read_text()))
    method = json.loads((REPO / 'configs/methods' / (arm + '.json')).read_text())
    cfg['arms'] = {arm: method['parameters']}
    cfg['official_method'] = method['official_method']
    cfg['adaptations'] = method['adaptations']
    cfg['spend'] = {'global_limit': '50.00'}
    return cfg
