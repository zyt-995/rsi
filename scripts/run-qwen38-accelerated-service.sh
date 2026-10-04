#!/usr/bin/env bash
# Separate inference deployment; old frozen service scripts are not edited.
set -euo pipefail
cards="${1:?GPU list required}"
port="${2:?port required}"
parallel="${3:?tensor parallel size required}"
mode="${4:?eager or graph required}"
case "$cards:$port:$parallel" in
  4,5,6,7:8050:4|0,1,2,3:8040:4|4,5,6,7:8041:4) ;;
  *) echo 'Unregistered service/GPU combination' >&2; exit 2 ;;
esac
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
export HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1
export VLLM_USE_FLASHINFER_SAMPLER=0
export CUDA_HOME=/data1/ytzhang/qwen38-runtime/venv/lib/python3.10/site-packages/nvidia/cu13
export CUDA_VISIBLE_DEVICES="$cards"
args=()
case "$mode" in
  eager) args+=(--enforce-eager) ;;
  graph) args+=(--compilation-config '{"mode":0,"cudagraph_mode":"FULL_DECODE_ONLY","cudagraph_capture_sizes":[1]}') ;;
  *) echo 'Unsupported execution mode' >&2; exit 2 ;;
esac
exec /data1/ytzhang/qwen38-runtime/venv/bin/python -m vllm.entrypoints.openai.api_server \
  --model /data3/ytzhang/models/Qwen3.8-27B --served-model-name Qwen/Qwen3.8-27B \
  --host 127.0.0.1 --port "$port" --api-key EMPTY \
  --dtype bfloat16 --tensor-parallel-size "$parallel" --max-model-len 262144 \
  --max-num-seqs 1 --gpu-memory-utilization 0.85 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 --no-enable-prefix-caching "${args[@]}"
