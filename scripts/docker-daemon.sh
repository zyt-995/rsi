#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script_path="$project_root/scripts/docker-daemon.sh"
if [[ -f "$project_root/.docker/active-overlay2" ]]; then
  default_runtime_dir=/tmp/rsi-harness-docker-overlay2
  default_data_root=/data2/ytzhang/rsi-harness-docker-overlay2
  default_storage_driver=overlay2
  default_session_suffix=-overlay2
else
  default_runtime_dir=/tmp/rsi-harness-docker
  default_data_root=/data1/ytzhang/rsi-harness-docker
  default_storage_driver=vfs
  default_session_suffix=
fi
runtime_dir="${RSI_DOCKER_RUNTIME_DIR:-$default_runtime_dir}"
data_root="${RSI_DOCKER_DATA_ROOT:-$default_data_root}"
storage_driver="${RSI_DOCKER_STORAGE_DRIVER:-$default_storage_driver}"
session_suffix="${RSI_DOCKER_SESSION_SUFFIX-$default_session_suffix}"
socket="$runtime_dir/docker.sock"
docker_bin="${RSI_DOCKER_BIN:-/home/ytzhang/budget-rl/.deps/docker-static/docker}"
dockerd_bin="${RSI_DOCKERD_BIN:-/home/ytzhang/budget-rl/.deps/docker-static/dockerd}"
slirp_bin="${RSI_SLIRP_BIN:-/home/ytzhang/budget-rl/.deps/docker-static/slirp4netns}"
runc_bin="${RSI_RUNC_ROOTLESS_BIN:-$project_root/scripts/runc-rootless.sh}"
proxy_url="${RSI_DOCKER_PROXY_URL:-http://10.0.2.2:17897}"
daemon_session="rsi-harness-dockerd${session_suffix}"
slirp_session="rsi-harness-slirp${session_suffix}"

is_ready() {
  [[ -S "$socket" ]] && "$docker_bin" -H "unix://$socket" version >/dev/null 2>&1
}

status() {
  if is_ready; then
    "$docker_bin" -H "unix://$socket" info \
      --format 'ready: server={{.ServerVersion}} driver={{.Driver}} root={{.DockerRootDir}}'
  else
    echo "stopped: unix://$socket"
    return 1
  fi
}

run_daemon() {
  printf '%s\n' "$$" >"$runtime_dir/unshare.pid"
  # Docker's integrated BuildKit resolves `runc` from PATH independently of
  # the daemon's named default runtime.  Point that lookup at the same wrapper
  # used by normal containers so rootless builds do not attempt host cgroups.
  buildkit_runtime_dir="$project_root/.deps/buildkit-rootless-runtime"
  mkdir -p "$buildkit_runtime_dir"
  ln -sfn "$runc_bin" "$buildkit_runtime_dir/runc"
  export PATH="$buildkit_runtime_dir:$(dirname "$dockerd_bin"):$PATH"
  if [[ "$proxy_url" != "none" ]]; then
    export HTTP_PROXY="$proxy_url" HTTPS_PROXY="$proxy_url" ALL_PROXY="$proxy_url"
    export http_proxy="$proxy_url" https_proxy="$proxy_url" all_proxy="$proxy_url"
    export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"
  fi
  exec unshare -Urnmpf --mount-proc /bin/sh -c \
    "mount -t tmpfs tmpfs /run &&
     mount -t tmpfs tmpfs /sys/kernel/security &&
     mkdir -p /run/systemd/resolve /run/docker /run/containerd &&
     touch /run/systemd/resolve/stub-resolv.conf &&
     mount --bind '$project_root/docker/resolv.conf' /run/systemd/resolve/stub-resolv.conf &&
     echo 1 > /proc/sys/net/ipv4/ip_forward &&
     exec '$dockerd_bin' --data-root '$data_root' --exec-root /run/docker \
       --pidfile /run/docker.pid --host 'unix://$socket' --storage-driver '$storage_driver' \
       --iptables=false --ip6tables=false --userland-proxy=false \
       --dns 10.0.2.3 --add-runtime 'rootless=$runc_bin' --default-runtime rootless"
}

run_slirp() {
  target_pid="$1"
  printf '%s\n' "$$" >"$runtime_dir/slirp.pid"
  exec "$slirp_bin" --configure --mtu=65520 \
    --userns-path="/proc/$target_pid/ns/user" "$target_pid" tap0
}

start() {
  if is_ready; then
    # The namespace-spawning parent can exit while dockerd itself remains
    # healthy. Accept the live daemon and its slirp helper in that case.
    if [[ -f "$runtime_dir/dockerd.pid" && -f "$runtime_dir/slirp.pid" ]] && \
       kill -0 "$(<"$runtime_dir/dockerd.pid")" 2>/dev/null && \
       kill -0 "$(<"$runtime_dir/slirp.pid")" 2>/dev/null; then
      status
      return
    fi
    echo "a Docker daemon is reachable at $socket but is not managed by this script" >&2
    exit 1
  fi
  for executable in "$docker_bin" "$dockerd_bin" "$slirp_bin" "$runc_bin"; do
    [[ -x "$executable" ]] || { echo "missing executable: $executable" >&2; exit 1; }
  done
  mkdir -p "$runtime_dir" "$data_root"

  command -v tmux >/dev/null || { echo "tmux is required to supervise the daemon" >&2; exit 1; }
  tmux kill-session -t "=$daemon_session" >/dev/null 2>&1 || true
  tmux kill-session -t "=$slirp_session" >/dev/null 2>&1 || true
  rm -f "$runtime_dir/unshare.pid" "$runtime_dir/slirp.pid" "$runtime_dir/dockerd.pid"
  tmux new-session -d -s "$daemon_session" \
    -e "RSI_DOCKER_RUNTIME_DIR=$runtime_dir" \
    -e "RSI_DOCKER_DATA_ROOT=$data_root" \
    -e "RSI_DOCKER_STORAGE_DRIVER=$storage_driver" \
    -e "RSI_DOCKER_SESSION_SUFFIX=$session_suffix" \
    "$script_path _daemon > '$runtime_dir/dockerd.log' 2>&1"

  for _ in $(seq 1 50); do
    if [[ -f "$runtime_dir/unshare.pid" ]]; then
      daemon_pid="$(<"$runtime_dir/unshare.pid")"
    else
      daemon_pid=""
    fi
    if [[ -r "/proc/$daemon_pid/uid_map" ]] && \
       awk '$1 == 0 && $3 == 1 { found=1 } END { exit !found }' "/proc/$daemon_pid/uid_map"; then
      break
    fi
    sleep 0.1
  done
  [[ "$daemon_pid" =~ ^[0-9]+$ ]] || { echo "daemon supervisor did not start" >&2; exit 1; }
  tmux new-session -d -s "$slirp_session" \
    -e "RSI_DOCKER_RUNTIME_DIR=$runtime_dir" \
    -e "RSI_DOCKER_SESSION_SUFFIX=$session_suffix" \
    "$script_path _slirp $daemon_pid > '$runtime_dir/slirp.log' 2>&1"

  for _ in $(seq 1 100); do
    if is_ready; then
      dockerd_pid="$(pgrep -P "$daemon_pid" -x dockerd | head -1 || true)"
      if [[ "$dockerd_pid" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$dockerd_pid" >"$runtime_dir/dockerd.pid"
      fi
      status
      return
    fi
    sleep 0.2
  done
  echo "project Docker daemon did not become ready; inspect $runtime_dir/dockerd.log" >&2
  exit 1
}

stop() {
  if [[ -f "$runtime_dir/dockerd.pid" ]]; then
    dockerd_pid="$(<"$runtime_dir/dockerd.pid")"
    if [[ "$dockerd_pid" =~ ^[0-9]+$ ]] && kill -0 "$dockerd_pid" 2>/dev/null; then
      kill "$dockerd_pid"
      for _ in $(seq 1 50); do
        kill -0 "$dockerd_pid" 2>/dev/null || break
        sleep 0.1
      done
    fi
  fi
  tmux kill-session -t "=$slirp_session" >/dev/null 2>&1 || true
  tmux kill-session -t "=$daemon_session" >/dev/null 2>&1 || true
  for pid_file in "$runtime_dir/slirp.pid" "$runtime_dir/unshare.pid" "$runtime_dir/dockerd.pid"; do
    if [[ -f "$pid_file" ]]; then
      pid="$(<"$pid_file")"
      if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
      fi
      rm -f "$pid_file"
    fi
  done
  echo "stopped: unix://$socket"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  _daemon) run_daemon ;;
  _slirp) run_slirp "${2:?missing daemon PID}" ;;
  *) echo "usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
