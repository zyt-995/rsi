#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$project_root/scripts/docker-env.sh"
harbor_root="$project_root/benchmarks/vendor/continual-skill-bench"

if [[ ! -x "$project_root/.venv-csb/bin/harbor" ]]; then
  "$project_root/scripts/setup-csb.sh"
fi

export PYTHONPATH="$harbor_root/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$project_root"
exec "$project_root/.venv-csb/bin/harbor" "$@"
