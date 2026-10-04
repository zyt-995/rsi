#!/usr/bin/env bash
# Explicit repair of known missing declared images; never replace ready images.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
source "$project_root/scripts/docker-env.sh"
dataset="$project_root/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo"
log_root="$project_root/runs/full-20260922-qwen38-27b/terminal/_supervisor/image-prep"
failed=0
for image in "$@"; do
    if ! rg -l --fixed-strings "docker_image = \"$image\"" "$dataset" -g task.toml >/dev/null; then
        echo "not a declared benchmark image: $image" >&2
        exit 2
    fi
    safe_name="${image//\//_}"
    safe_name="${safe_name//:/_}"
    (
        flock -x 9
        if docker image inspect "$image" >/dev/null 2>&1; then
            echo "already exists; leaving untouched: $image"
            exit 0
        fi
        case "$image" in
            alexgshaw/*) source_image="docker.1ms.run/$image" ;;
            *) source_image="$image" ;;
        esac
        echo "$(date -Is) repair start $image with digest pinning and HTTP/1.1"
        GODEBUG=http2client=0 "$project_root/scripts/normalize-image-for-singleuid.sh" "$source_image" "$image"
    ) 9>"$log_root/$safe_name.lock" >>"$log_root/$safe_name.repair.log" 2>&1 && rc=0 || rc=$?
    echo "$(date -Is) repair finished image=$image exit=$rc"
    [ "$rc" -eq 0 ] || failed=1
done
exit "$failed"
