#!/bin/bash
mkdir -p /logs/verifier
python3 - <<'PY'
import json
from pathlib import Path
try:
    passed = json.loads(Path('/app/rsi-report.json').read_text()) == {'status': 'done', 'checks': 2}
except Exception:
    passed = False
Path('/logs/verifier/reward.txt').write_text(str(int(passed)))
Path('/logs/verifier/ctrf.json').write_text(json.dumps({'results': {'summary': {'tests': 1, 'passed': int(passed), 'failed': int(not passed), 'skipped': 0, 'pending': 0, 'other': 0}, 'tests': [{'name': 'json-contract', 'status': 'passed' if passed else 'failed'}]}}))
PY
