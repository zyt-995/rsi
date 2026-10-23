#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${RSI_PYTHON:-python3.12}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
if [ ! -x .venv/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python scripts/bootstrap.py --upstreams
.venv/bin/python -m pip install -r requirements.lock -e upstream/harbor -e .
.venv/bin/python -m pip check
.venv/bin/python scripts/doctor.py
