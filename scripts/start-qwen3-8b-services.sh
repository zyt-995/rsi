#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="/data1/ytzhang/qwen38-runtime/venv/bin/python"
model_path="/data3/ytzhang/models/Qwen3-8B"
log_root="$project_root/runs/model-services/qwen3-8b"
mkdir -p "$log_root"

start_service() {
    local session="$1"
    local card="$2"
    local port="$3"
    local log="$log_root/$session.log"
    if tmux has-session -t "$session" 2>/dev/null; then
        echo "$session already running"
        return
    fi
    tmux new-session -d -s "$session" \
        "unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; \
         export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost; \
         export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1; \
         export HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1; \
         export VLLM_USE_FLASHINFER_SAMPLER=0; \
         export CUDA_HOME=/data1/ytzhang/qwen38-runtime/venv/lib/python3.10/site-packages/nvidia/cu13; \
         export CUDA_VISIBLE_DEVICES='$card'; \
         exec '$python_bin' -m vllm.entrypoints.openai.api_server \
           --model '$model_path' \
           --served-model-name Qwen/Qwen3-8B \
           --host 127.0.0.1 --port '$port' --api-key EMPTY \
           --dtype bfloat16 --tensor-parallel-size 1 --max-model-len 131072 \
           --max-num-seqs 1 --gpu-memory-utilization 0.85 \
           --enable-auto-tool-choice --tool-call-parser qwen3_coder \
           --reasoning-parser qwen3 --enforce-eager --no-enable-prefix-caching \
           2>&1 | tee -a '$log'"
    echo "started $session on GPU $card port $port"
}

start_service qwen3-8b-service-2 2 8052
start_service qwen3-8b-service-3 3 8053
start_service qwen3-8b-service-6 6 8056
start_service qwen3-8b-service-7 7 8057
