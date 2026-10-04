#!/usr/bin/env bash
set -euo pipefail
# Restores only the failed 8040 service. Healthy endpoints are left alone.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1
export VLLM_USE_FLASHINFER_SAMPLER=0
export CUDA_HOME=/data1/ytzhang/qwen38-runtime/venv/lib/python3.10/site-packages/nvidia/cu13
export CUDA_VISIBLE_DEVICES=0,1
# Leave headroom for the other user's existing GPU-1 process (~6 GiB).
exec /data1/ytzhang/qwen38-runtime/venv/bin/python -m vllm.entrypoints.openai.api_server \
    --model /data3/ytzhang/models/Qwen3.8-27B \
    --served-model-name Qwen/Qwen3.8-27B \
    --host 127.0.0.1 --port 8040 --api-key EMPTY \
    --dtype bfloat16 --tensor-parallel-size 2 --max-model-len 262144 \
    --max-num-seqs 1 --gpu-memory-utilization 0.85 \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder \
    --reasoning-parser qwen3 --enforce-eager --no-enable-prefix-caching
