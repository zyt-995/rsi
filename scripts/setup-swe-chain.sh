#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
swe_root="$project_root/benchmarks/vendor/swe-chain"
uv_bin="${RSI_UV_BIN:-$project_root/.deps/uv-bin/uv}"
python_bin="${RSI_PYTHON_BIN:-$project_root/.deps/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12}"
container_proxy="${RSI_DOCKER_PROXY_URL:-http://10.0.2.2:17897}"
source "$project_root/scripts/docker-env.sh"

if [[ ! -x "$project_root/.venv-swe/bin/python" ]]; then
  "$uv_bin" venv --python "$python_bin" "$project_root/.venv-swe"
fi
"$uv_bin" pip install --python "$project_root/.venv-swe/bin/python" \
  --requirement "$swe_root/requirements.txt"

if ! compgen -G "$swe_root/data/*_specs_chain.jsonl" >/dev/null || \
   ! compgen -G "$swe_root/oracle/*/*/*.json" >/dev/null; then
  (cd "$swe_root" && "$project_root/.venv-swe/bin/python" load_dataset.py)
fi

if [[ "${1:-}" == "--smoke-image" ]] && ! docker image inspect swe-chain:attrs-21.3.0 >/dev/null 2>&1; then
  (
    cd "$swe_root"
    "$project_root/.venv-swe/bin/python" -c \
      'from packaging.version import Version; from fetchers.package import PackageExtractor; PackageExtractor("attrs").download_from_pypi(Version("21.3.0"), ".packages")'
    docker build --network host \
      --build-arg "HTTP_PROXY=$container_proxy" \
      --build-arg "HTTPS_PROXY=$container_proxy" \
      --build-arg NO_PROXY=localhost,127.0.0.1 \
      --tag swe-chain:attrs-21.3.0 \
      --file dockerfiles/attrs-23.Dockerfile \
      .packages/attrs/21.3.0
  )
fi

echo "SWE-Chain dependencies and released data ready"
