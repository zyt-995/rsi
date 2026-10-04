#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ] && [ "$#" -ne 5 ]; then
    echo "usage: $0 {terminus2_baseline|terminus2_evomem|terminus2_self_action|terminus2_open_ended} {8040|8041|8042|8043|8044} CHAIN TRIALS_DIR [STAGE_INDEX]" >&2
    exit 2
fi

variant="$1"
port="$2"
chain="$3"
trials_dir="$4"
stage_args=()
if [ "$#" -eq 5 ]; then
    if ! [[ "$5" =~ ^[1-9][0-9]*$ ]]; then
        echo "STAGE_INDEX must be a positive integer" >&2
        exit 2
    fi
    stage_args=(--start-index "$5" --max-tasks 1)
fi
case "$variant" in
    terminus2_baseline|terminus2_evomem|terminus2_self_action|terminus2_open_ended) ;;
    *) echo "unsupported variant: $variant" >&2; exit 2 ;;
esac
case "$port" in
    8040|8041|8042|8043|8044) ;;
    *) echo "unsupported local endpoint port: $port" >&2; exit 2 ;;
esac

project_root="$(cd "$(dirname "$0")/.." && pwd)"
export HARBOR_TMUX_BUNDLE="${HARBOR_TMUX_BUNDLE:-$project_root/.cache/tmux/tmux-runtime-singleuid-amd64.tar.gz}"
terminal_root="$project_root/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo"
runner="$terminal_root/harbor_EvoMem/scripts/run_chain.py"
dataset="$terminal_root/Terminal-Bench-Evo"
harbor_bin="$project_root/.venv-terminal/bin/harbor"

if [ ! -d "$dataset/$chain" ]; then
    echo "unknown Terminal-Bench-Evo chain: $chain" >&2
    exit 2
fi

source "$project_root/scripts/docker-env.sh"
export PATH="$project_root/.venv-terminal/bin:$PATH"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export HARBOR_EVOMEM_LLM_ENV="$terminal_root/harbor_EvoMem/scripts/terminus2_llm_${port}.env"
export HARBOR_FORMAL_REQUIRE_CLEAN_VERIFIER=1
export HARBOR_VERIFIER_BUNDLE="$project_root/.cache/tmux/verifier-runtime-singleuid-amd64.tar.gz"

exec python "$runner" \
    --dataset "$dataset" \
    --chain "$chain" \
    --variant "$variant" \
    --trials-dir "$trials_dir" \
    "${stage_args[@]}" \
    --resume \
    --no-stop-on-failure \
    --ae GIT_PAGER=cat \
    --ae PAGER=cat \
    --ae HTTP_PROXY=http://10.0.2.2:17897 \
    --ae HTTPS_PROXY=http://10.0.2.2:17897 \
    --ae http_proxy=http://10.0.2.2:17897 \
    --ae https_proxy=http://10.0.2.2:17897 \
    --ae NO_PROXY=127.0.0.1,localhost \
    --ae no_proxy=127.0.0.1,localhost \
    --kwarg record_terminal_session=false \
    --kwarg max_turns=15 \
    --kwarg suppress_max_turns_warning=true \
    --kwarg max_output_retries=1 \
    --kwarg 'llm_call_kwargs={"max_tokens":32768,"timeout":2700}' \
    --harbor-bin "$harbor_bin" \
    -- \
    --ve HTTP_PROXY=http://10.0.2.2:17897 \
    --ve HTTPS_PROXY=http://10.0.2.2:17897 \
    --ve http_proxy=http://10.0.2.2:17897 \
    --ve https_proxy=http://10.0.2.2:17897 \
    --ve NO_PROXY=127.0.0.1,localhost \
    --ve no_proxy=127.0.0.1,localhost \
    --ve TAR_OPTIONS=--no-same-owner \
    --ve PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    --ve UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    --agent-timeout 7200 \
    --verifier-timeout 600
