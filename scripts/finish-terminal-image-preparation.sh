#!/usr/bin/env bash
# One bounded recovery pass after the old bulk downloader/explicit repairs end.
# Existing normalised images are skipped and per-image locks prevent duplicates.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
while tmux has-session -t rsi-terminal-full-image-prep 2>/dev/null \
    || tmux has-session -t rsi-terminal-image-repairs 2>/dev/null; do
    sleep 20
done
export GODEBUG="${GODEBUG:+$GODEBUG,}http2client=0"
exec bash "$project_root/scripts/prepare-terminal-full-images.sh" 2
