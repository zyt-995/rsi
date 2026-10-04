#!/usr/bin/env bash
# Source this file before invoking an upstream benchmark directly.
_rsi_docker_dir="${RSI_DOCKER_BIN:-$HOME/budget-rl/.deps/docker-static/docker}"
if [[ -x "$_rsi_docker_dir" ]]; then
  export PATH="$(dirname "$_rsi_docker_dir"):$PATH"
fi
_rsi_project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_rsi_project_socket="/tmp/rsi-harness-docker/docker.sock"
_rsi_overlay_socket="/tmp/rsi-harness-docker-overlay2/docker.sock"
_rsi_overlay_marker="$_rsi_project_root/.docker/active-overlay2"
_rsi_fallback_socket="/tmp/${USER}-docker-unshare/docker.sock"
if [[ -f "$_rsi_overlay_marker" ]]; then
  # Once cut over, fail closed if overlay2 is down; never silently write to VFS.
  export DOCKER_HOST="unix://${_rsi_overlay_socket}"
elif [[ -S "$_rsi_project_socket" ]]; then
  export DOCKER_HOST="unix://${_rsi_project_socket}"
elif [[ -S "$_rsi_fallback_socket" ]]; then
  export DOCKER_HOST="unix://${_rsi_fallback_socket}"
fi
if [[ -x "$_rsi_project_root/.docker/cli-plugins/docker-compose" ]]; then
  export DOCKER_CONFIG="$_rsi_project_root/.docker"
fi
unset _rsi_docker_dir _rsi_project_socket _rsi_overlay_socket _rsi_overlay_marker _rsi_fallback_socket _rsi_project_root
