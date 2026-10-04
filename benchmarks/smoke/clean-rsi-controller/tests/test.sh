#!/bin/bash
mkdir -p /logs/verifier
python3 - <<'PY'
import json
from pathlib import Path
try:
    passed = Path('/app/move.txt').read_text() == 'e2e4\n'
except Exception:
    passed = False
Path('/logs/verifier/reward.txt').write_text(str(int(passed)))
Path('/logs/verifier/ctrf.json').write_text(json.dumps({'results': {'summary': {'tests': 1, 'passed': int(passed), 'failed': int(not passed), 'skipped': 0, 'pending': 0, 'other': 0}, 'tests': [{'name': 'move-file-contract', 'status': 'passed' if passed else 'failed'}]}}))
PY
