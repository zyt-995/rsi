#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_bin="${RSI_UV_BIN:-$project_root/.deps/uv-bin/uv}"
python_bin="${RSI_PYTHON_BIN:-$project_root/.deps/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12}"
harbor_root="$project_root/benchmarks/vendor/continual-skill-bench"
compat_patch="$project_root/benchmarks/patches/continual-skill-bench-rootless.patch"

[[ -x "$uv_bin" ]] || { echo "missing uv executable: $uv_bin" >&2; exit 1; }
[[ -x "$python_bin" ]] || { echo "missing Python executable: $python_bin" >&2; exit 1; }

if git -C "$harbor_root" apply --check "$compat_patch" >/dev/null 2>&1; then
  git -C "$harbor_root" apply "$compat_patch"
elif ! git -C "$harbor_root" apply --reverse --check "$compat_patch" >/dev/null 2>&1; then
  echo "ContinualSkillBench compatibility patch is neither cleanly applicable nor already applied" >&2
  exit 1
fi

if [[ ! -x "$project_root/.venv-csb/bin/python" ]]; then
  "$uv_bin" venv --python "$python_bin" "$project_root/.venv-csb"
fi
"$uv_bin" pip install --python "$project_root/.venv-csb/bin/python" \
  --requirement "$project_root/docker/csb-requirements.txt"

PYTHONPATH="$project_root/benchmarks/vendor/continual-skill-bench/src" \
  "$project_root/.venv-csb/bin/python" -c \
  'import datasets, litellm, pydantic, typer; import harbor; print("ContinualSkillBench dependencies ready")'
