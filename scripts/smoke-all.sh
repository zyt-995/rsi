#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$project_root"

scripts/setup-all.sh
python3 -m unittest discover -s tests -q
python3 -m rsi_harness matrix \
  --output "runs/smoke-$stamp/six-modes" --tasks 6 --shift-every 2 \
  --budget 180 --self-budget 80 --max-steps 6 \
  --improvement-steps 1 --evolution-candidates 2 --seeds 0
scripts/csb.sh jobs start --job-name "csb-offline-$stamp" \
  --jobs-dir "runs/smoke-$stamp/csb" \
  --path benchmarks/smoke/csb-hello-world --agent oracle \
  --n-concurrent 1 --max-retries 0 --enable-verification --quiet
scripts/swe-chain.sh smoke

python3 - <<'PY'
from rsi_harness.benchmarks.personamem import PersonaMemEvo

benchmark = PersonaMemEvo(
    "benchmarks/vendor/evoarena/EvoMem-PersonaMem-Evo", limit=1, seed=0
)
observation = benchmark.begin(benchmark.tasks()[0])
assert observation["options"] and observation["retrieved_history"]
print("PersonaMem-Evo released-data adapter smoke: ready")
PY

echo "All smoke checks passed: runs/smoke-$stamp"
