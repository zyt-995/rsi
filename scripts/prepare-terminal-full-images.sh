#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dataset="$project_root/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo"
normalize="$project_root/scripts/normalize-image-for-singleuid.sh"
formal_root="$project_root/runs/full-20260922-qwen38-27b/terminal"
log_root="$formal_root/_supervisor/image-prep"
ready_marker="$formal_root/_supervisor/images-ready"
jobs="${1:-4}"

mkdir -p "$log_root"
rm -f "$ready_marker"
source "$project_root/scripts/docker-env.sh"

is_normalized() {
    local image="$1"
    [ "$(docker image inspect "$image" --format '{{if .Config.Labels}}{{index .Config.Labels "rsi.single_uid_normalized"}}{{end}}' 2>/dev/null || true)" = "true" ]
}

prepare_pair() {
    local source_image="$1"
    local target_image="$2"
    local safe_name="${target_image//\//_}"
    safe_name="${safe_name//:/_}"
    if is_normalized "$target_image"; then
        echo "ready: $target_image"
        return 0
    fi
    echo "normalizing: $source_image -> $target_image"
    # Per-image lock also covers explicit repair attempts while the bulk
    # preparer is alive. Keep historical errors for diagnosis.
    (
        flock -x 9
        is_normalized "$target_image" || "$normalize" "$source_image" "$target_image"
    ) 9>"$log_root/$safe_name.lock" >>"$log_root/$safe_name.log" 2>&1
    is_normalized "$target_image"
}

pairs=(
    "docker.m.daocloud.io/library/debian:13.0-slim|debian:13.0-slim"
    "docker.m.daocloud.io/library/debian:bookworm-slim|debian:bookworm-slim"
    "docker.m.daocloud.io/library/debian:bullseye-slim|debian:bullseye-slim"
    "docker.m.daocloud.io/library/python:3.10-slim-bookworm|python:3.10-slim-bookworm"
    "docker.m.daocloud.io/library/python:3.11|python:3.11"
    "docker.m.daocloud.io/library/python:3.11-slim|python:3.11-slim"
    "docker.m.daocloud.io/library/python:3.11-slim-bookworm|python:3.11-slim-bookworm"
    "docker.m.daocloud.io/library/python:3.12-slim|python:3.12-slim"
    "docker.m.daocloud.io/library/python:3.12-slim-bookworm|python:3.12-slim-bookworm"
    "docker.m.daocloud.io/library/python:3.13-slim-bookworm|python:3.13-slim-bookworm"
    "docker.m.daocloud.io/library/ubuntu:22.04|ubuntu:22.04"
    "docker.m.daocloud.io/library/ubuntu:24.04|ubuntu:24.04"
)

while IFS= read -r image; do
    [ -n "$image" ] || continue
    case "$image" in
        rsi-terminal/*) continue ;;
        alexgshaw/*) pairs+=("docker.1ms.run/$image|$image"); continue ;;
    esac
    pairs+=("$image|$image")
done < <(
    sed -n 's/^[[:space:]]*docker_image[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$dataset"/*/task.toml | sort -u
)

failed=0
batch_pids=()
batch_names=()
wait_batch() {
    local index
    for index in "${!batch_pids[@]}"; do
        if ! wait "${batch_pids[$index]}"; then
            echo "image preparation failed: ${batch_names[$index]}" >&2
            failed=1
        fi
    done
    batch_pids=()
    batch_names=()
}

for pair in "${pairs[@]}"; do
    source_image="${pair%%|*}"
    target_image="${pair#*|}"
    (
        prepare_pair "$source_image" "$target_image" || exit 1
        case "$target_image" in
            debian:*|python:*|ubuntu:*) "$project_root/scripts/prepare-singleuid-base.sh" "$target_image" ;;
        esac
    ) &
    batch_pids+=("$!")
    batch_names+=("$target_image")
    if [ "${#batch_pids[@]}" -ge "$jobs" ]; then
        wait_batch
    fi
done
wait_batch

[ "$failed" -eq 0 ] || exit 1
docker tag ubuntu:24.04 library/ubuntu:24.04
touch "$ready_marker"
echo "all Terminal-Bench-Evo images are ready"
