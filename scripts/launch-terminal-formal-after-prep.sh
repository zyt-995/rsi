#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prep_session="rsi-terminal-image-prep"
formal_session="rsi-terminal-diverse-formal"
formal_root="$project_root/runs/formal-20260922-qwen38-27b/terminal"
prebuild_log="$project_root/runs/formal-20260922-qwen38-27b/image-prebuild.log"

while tmux has-session -t "$prep_session" 2>/dev/null; do
    sleep 20
done

source "$project_root/scripts/docker-env.sh"
required_images=(
    ubuntu:22.04
    python:3.13-slim-bookworm
)
for image in "${required_images[@]}"; do
    normalized="$(docker image inspect "$image" --format '{{index .Config.Labels "rsi.single_uid_normalized"}}' 2>/dev/null || true)"
    if [ "$normalized" != "true" ]; then
        echo "image preparation failed or incomplete: $image" >&2
        exit 1
    fi
done

"$project_root/scripts/prebuild-terminal-diverse-images.sh" 2>&1 | tee -a "$prebuild_log"

if tmux has-session -t "$formal_session" 2>/dev/null; then
    echo "$formal_session already exists" >&2
    exit 1
fi
tmux new-session -d -s "$formal_session" \
    "$project_root/scripts/supervise-terminal-diverse.py --root '$formal_root' --hours 10 --monitor-seconds 1800 --poll-seconds 20 2>&1 | tee -a '$formal_root/_supervisor/supervisor.log'"
echo "started $formal_session"
