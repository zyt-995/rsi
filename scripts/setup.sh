#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${RSI_PYTHON:-python3.12}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:3] == (3, 12, 14), "Python 3.12.14 required to match source runtime"'
if [ ! -x .venv/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python scripts/bootstrap.py --upstreams
.venv/bin/python -m pip install -r requirements.lock -e upstream/harbor -e .
.venv/bin/python -m pip check
.venv/bin/python scripts/check_reproduction.py --software-only
.venv/bin/python scripts/doctor.py
