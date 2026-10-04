#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

scripts/docker-daemon.sh start
source scripts/docker-env.sh
scripts/setup-csb.sh
scripts/setup-swe-chain.sh --smoke-image
python3 -m rsi_harness preflight
