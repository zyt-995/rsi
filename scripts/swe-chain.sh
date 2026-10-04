#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$project_root/scripts/docker-env.sh"
swe_root="$project_root/benchmarks/vendor/swe-chain"
export PATH="$project_root/.venv-swe/bin:$PATH"

command_name="${1:-list}"
if [[ $# -gt 0 ]]; then shift; fi
case "$command_name" in
  list)
    for file in "$swe_root"/data/*_specs_chain.jsonl; do
      printf '%s\t%s steps\n' "$(basename "$file")" "$(wc -l < "$file")"
    done
    ;;
  run)
    if [[ $# -lt 4 ]]; then
      echo "usage: scripts/swe-chain.sh run <data-jsonl> <agent> <provider> <model> [effort]" >&2
      exit 2
    fi
    (cd "$swe_root" && bash run.sh "$@")
    ;;
  eval)
    if [[ $# -lt 1 ]]; then
      echo "usage: scripts/swe-chain.sh eval <chain.json> [workers] [curr-source] [--resume]" >&2
      exit 2
    fi
    (cd "$swe_root" && bash eval.sh "$@")
    ;;
  smoke)
    "$project_root/scripts/setup-swe-chain.sh" --smoke-image
    docker run --rm --network none swe-chain:attrs-21.3.0 \
      python -c 'import attrs, pytest; print(f"attrs={attrs.__version__} pytest={pytest.__version__}")'
    docker run --rm --network none swe-chain:attrs-21.3.0 \
      pytest -q tests/test_make.py -x --disable-warnings
    ;;
  *)
    echo "unknown command: $command_name (expected list, run, eval, smoke)" >&2
    exit 2
    ;;
esac
