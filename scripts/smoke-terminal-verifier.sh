#!/usr/bin/env bash
# Exercise the actual verifier bootstrap on a fresh, unsolved image. A zero
# reward is expected; success here means pytest ran and emitted its report.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
source "$root/scripts/docker-env.sh"
task="${1:?usage: smoke-terminal-verifier.sh TASK IMAGE OUTPUT_DIR}"
image="${2:?missing image}"
output="${3:?missing output directory}"
tests="$root/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo/$task/tests"
mkdir -p "$output"
output="$(realpath "$output")"
container="$(docker create --network host \
    -e HTTP_PROXY=http://10.0.2.2:17897 -e HTTPS_PROXY=http://10.0.2.2:17897 \
    -e http_proxy=http://10.0.2.2:17897 -e https_proxy=http://10.0.2.2:17897 \
    -e PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    -e UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    -e TAR_OPTIONS=--no-same-owner \
    -v "$tests:/tests:ro" -v "$output:/logs/verifier" \
    "$image" sleep infinity)"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
docker start "$container" >/dev/null
docker exec -i "$container" tar --no-same-owner -xzf - -C / \
    < "$root/.cache/tmux/tmux-runtime-singleuid-amd64.tar.gz"
docker exec -i "$container" tar --no-same-owner -xzf - -C / \
    < "$root/.cache/tmux/verifier-runtime-singleuid-amd64.tar.gz"
timeout 600 docker exec "$container" bash -c 'export PATH=/opt/rsi-verifier/bootstrap:$PATH; bash /tests/test.sh' > "$output/test-stdout.txt" 2>&1
python3 - "$output" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
report = json.loads((root / 'ctrf.json').read_text())
summary = report['results']['summary']
assert summary['tests'] > 0, summary
print('verifier smoke: tests executed', summary)
PY
