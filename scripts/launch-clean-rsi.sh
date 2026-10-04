#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
source "$project_root/scripts/docker-env.sh"
cd "$project_root"
export PYTHONPATH="$project_root${PYTHONPATH:+:$PYTHONPATH}"
exec "$project_root/.venv-terminal/bin/python" -u -m rsi_harness.clean_scheduler "$@"
