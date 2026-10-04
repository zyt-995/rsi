#!/bin/bash
set -euo pipefail

reward=0
if [[ -f /app/hello.txt ]] && [[ "$(cat /app/hello.txt)" == "Hello, world!" ]]; then
  reward=1
fi
printf '%s\n' "$reward" > /logs/verifier/reward.txt
