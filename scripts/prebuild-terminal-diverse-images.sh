#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dataset="$project_root/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo"
source "$project_root/scripts/docker-env.sh"

chains=(
    llm-inference-batching-scheduler
    adaptive-rejection-sampler
    cancel-async-tasks
    model-extraction-relu-logits
    regex-chess
)
proxy="http://10.0.2.2:17897"

build_image() {
    local context="$1"
    local tag="$2"
    docker build \
        --network host \
        --build-arg "HTTP_PROXY=$proxy" \
        --build-arg "HTTPS_PROXY=$proxy" \
        --build-arg "ALL_PROXY=$proxy" \
        --build-arg "http_proxy=$proxy" \
        --build-arg "https_proxy=$proxy" \
        --build-arg "all_proxy=$proxy" \
        --tag "$tag" \
        "$context"
}

for chain in "${chains[@]}"; do
    prototype_tag="alexgshaw/$chain:20251031"
    echo "prebuilding $chain prototype as $prototype_tag"
    build_image "$dataset/$chain/environment" "$prototype_tag"
    for task_dir in "$dataset/${chain}"-EVO-*; do
        [ -f "$task_dir/environment/Dockerfile" ] || continue
        task="$(basename "$task_dir")"
        tag="rsi-prebuild/${task,,}:v1"
        echo "prebuilding $task as $tag"
        build_image "$task_dir/environment" "$tag"
    done
done

echo "all selected Terminal-Bench-Evo variant environments built successfully"
