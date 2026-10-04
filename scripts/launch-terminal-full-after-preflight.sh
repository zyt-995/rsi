#!/usr/bin/env bash
# Compatibility entry point: all full launches now use the guarded preflight gate.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$project_root/scripts/launch-terminal-infrastructure.sh"
