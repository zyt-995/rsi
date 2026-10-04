#!/usr/bin/env bash
set -euo pipefail

project=/home/ytzhang/rsi-harness
run_root="$project/runs/clean-rsi-20260923-qwen38-27b"
review_dir="$run_root/codex_reviews"
prompt="$project/docs/clean_rsi_codex_review_prompt.md"
export PATH="/home/ytzhang/.local/bin:$PATH"

umask 077
mkdir -p "$review_dir"
exec 9>"$review_dir/review.lock"
if ! flock -n 9; then
    exit 0
fi

if python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("terminal") == "complete")' "$run_root/status.json" 2>/dev/null | grep -qx True; then
    exit 0
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
log="$review_dir/$stamp.log"
summary="$review_dir/$stamp.summary.md"
printf 'Started at %s\n' "$stamp" >"$log"
set +e
timeout --signal=TERM --kill-after=30s 20m \
    /home/ytzhang/.local/bin/codex exec \
    --approve-for-me \
    --config 'model_reasoning_effort="medium"' \
    --skip-git-repo-check \
    --cd "$project" \
    --output-last-message "$summary" \
    - <"$prompt" >>"$log" 2>&1
result=$?
set -e
printf '\nExit code: %s at %s\n' "$result" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$log"
exit "$result"
