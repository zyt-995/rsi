#!/usr/bin/env bash
# Two independently supervised control planes; neither owns model workers.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
run_root="$project_root/runs/full-20260922-qwen38-27b/terminal"
source "$project_root/scripts/docker-env.sh"
mkdir -p "$run_root/_supervisor" "$run_root/_infra"
cd "$project_root"

case "${1:-launch}" in
    _guard)
        while :; do
            "$project_root/.venv-terminal/bin/python" -u \
                "$project_root/scripts/watch-terminal-infrastructure.py" --root "$run_root" \
                >> "$run_root/_supervisor/watchdog.log" 2>&1 && rc=0 || rc=$?
            echo "$(date -Is) watchdog exited rc=$rc; restart after 15s" >> "$run_root/_supervisor/watchdog.log"
            sleep 15
        done
        ;;
    _preflight)
        while :; do
            "$project_root/.venv-terminal/bin/python" -u \
                "$project_root/scripts/preflight-terminal-environments.py" --root "$run_root" --jobs 2 \
                >> "$run_root/_infra/preflight-service.log" 2>&1 && rc=0 || rc=$?
            echo "$(date -Is) preflight exited rc=$rc; restart after 30s" >> "$run_root/_infra/preflight-service.log"
            sleep 30
        done
        ;;
    launch)
        for role in guard preflight; do
            session="rsi-terminal-infra-$role"
            if ! tmux has-session -t "$session" 2>/dev/null; then
                tmux new-session -d -s "$session" "bash '$project_root/scripts/launch-terminal-infrastructure.sh' '_$role'"
            fi
        done
        echo "Infrastructure guard and preflight services started (or already present)."
        ;;
    *) echo "unsupported command: $1" >&2; exit 2 ;;
esac
