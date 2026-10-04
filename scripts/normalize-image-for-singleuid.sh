#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: $0 SOURCE_IMAGE [TARGET_IMAGE]" >&2
    exit 2
fi

source_image="$1"
target_image="${2:-$1}"
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
crane_bin="/home/ytzhang/budget-rl/.deps/docker-static/crane"
work_parent="${RSI_IMAGE_WORK_DIR:-/data2/ytzhang/rsi-image-normalize}"
mkdir -p "$work_parent"
work_dir="$(mktemp -d "$work_parent/job.XXXXXX")"

cleanup() {
    case "$work_dir" in
        "$work_parent"/job.*) rm -rf -- "$work_dir" ;;
        *) echo "refusing to remove unexpected temporary path: $work_dir" >&2 ;;
    esac
}
trap cleanup EXIT

source "$project_root/scripts/docker-env.sh"
docker_data_root="$(docker info --format '{{.DockerRootDir}}')"
# Refuse new work before consuming the safety reserve. Never prune images or
# other users' data to make space automatically.
python3 - "$work_parent" "$docker_data_root" <<'PY'
import shutil
import sys
for path, reserve in ((sys.argv[1], 50), (sys.argv[2], 100), ('/data1', 10)):
    if shutil.disk_usage(path).free < reserve * 1024**3:
        raise SystemExit(f'insufficient free space on {path}; reserve={reserve} GiB')
PY

# tmux servers may predate the interactive shell's proxy environment.  Crane
# runs on the host (not in the Docker network namespace), so pin the host-side
# proxy explicitly for reproducible registry access.
export HTTP_PROXY="${RSI_HOST_PROXY_URL:-http://127.0.0.1:17897}"
export HTTPS_PROXY="$HTTP_PROXY"
export ALL_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTP_PROXY"
export all_proxy="$HTTP_PROXY"
mkdir -p "$work_dir/rootfs"

retry() {
    local attempt
    for attempt in 1 2 3; do
        # The registry proxy has repeatedly reset HTTP/2 streams. Switch
        # transport on recovery rather than repeating the identical failure.
        local transport="${GODEBUG:-}"
        if [ "$attempt" -gt 1 ]; then
            transport="${transport:+$transport,}http2client=0"
        fi
        if timeout --signal=TERM --kill-after=30 2400 env GODEBUG="$transport" "$@"; then
            return 0
        fi
        echo "attempt $attempt failed (next transport HTTP/1.1): $*" >&2
        [ "$attempt" -eq 3 ] || sleep 10
    done
    return 1
}

echo "resolving immutable digest for $source_image"
digest="$(retry "$crane_bin" digest --platform linux/amd64 "$source_image")"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid registry digest" >&2; exit 1; }
pinned_image="${source_image%@*}@$digest"
echo "exporting $pinned_image"
retry "$crane_bin" export --platform linux/amd64 "$pinned_image" "$work_dir/rootfs.tar"
# Each attempt writes a fresh file: partial JSON from a failed request must
# never be concatenated with a successful retry.
for attempt in 1 2 3; do
    if timeout --signal=TERM --kill-after=30 120 env GODEBUG=http2client=0 \
        "$crane_bin" config --platform linux/amd64 "$pinned_image" > "$work_dir/config.json.tmp"; then
        mv "$work_dir/config.json.tmp" "$work_dir/config.json"
        break
    fi
done
[ -s "$work_dir/config.json" ] || exit 1

echo "flattening ownership for single-UID Docker"
tar --no-same-owner -xf "$work_dir/rootfs.tar" -C "$work_dir/rootfs"

# The project Docker daemon intentionally maps only the invoking host UID.  Apt's
# default `_apt` privilege drop therefore cannot switch to the image's numeric
# `_apt` UID while a task Dockerfile is being built.  Keeping apt as container
# root is equivalent inside this isolated user namespace and lets EVO images be
# built from the flattened base image.
if [ -d "$work_dir/rootfs/etc/apt/apt.conf.d" ]; then
    printf 'APT::Sandbox::User "root";\n' \
        > "$work_dir/rootfs/etc/apt/apt.conf.d/99-rsi-singleuid"
    chmod 0644 "$work_dir/rootfs/etc/apt/apt.conf.d/99-rsi-singleuid"
fi

mapfile -d '' changes < <(
    python3 - "$work_dir/config.json" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8")).get("config") or {}
values = []
for value in config.get("Env") or []:
    values.append(f"ENV {value}")
if config.get("WorkingDir"):
    values.append(f"WORKDIR {config['WorkingDir']}")
if config.get("User"):
    values.append(f"USER {config['User']}")
if config.get("Entrypoint") is not None:
    values.append("ENTRYPOINT " + json.dumps(config["Entrypoint"], separators=(",", ":")))
if config.get("Cmd") is not None:
    values.append("CMD " + json.dumps(config["Cmd"], separators=(",", ":")))
for port in sorted((config.get("ExposedPorts") or {}).keys()):
    values.append(f"EXPOSE {port}")
for volume in sorted((config.get("Volumes") or {}).keys()):
    values.append(f"VOLUME {volume}")
values.append("LABEL rsi.single_uid_normalized=true")
sys.stdout.buffer.write(b"".join(value.encode() + b"\0" for value in values))
PY
)

import_args=()
import_args+=(--change "LABEL rsi.source_digest=$digest")
for change in "${changes[@]}"; do
    import_args+=(--change "$change")
done
echo "importing normalized image as $target_image"
python3 - "$work_dir/rootfs.tar" "$docker_data_root" <<'PY'
import os
import shutil
import sys
required = 100 * 1024**3 + 2 * os.path.getsize(sys.argv[1])
if shutil.disk_usage(sys.argv[2]).free < required:
    raise SystemExit('image import would consume Docker disk safety reserve')
PY
tar --numeric-owner --owner=0 --group=0 -C "$work_dir/rootfs" -cf - . \
    | docker import "${import_args[@]}" - "$target_image" >/dev/null
docker image inspect "$target_image" \
    --format 'ready: {{index .Config.Labels "rsi.single_uid_normalized"}} {{.RepoTags}} {{.Size}} bytes'
