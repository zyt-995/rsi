#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 {terminus2_baseline|terminus2_evomem|terminus2_self_action|terminus2_open_ended|terminus2_evolution} {8040|8041|8042|8043|8044} TRIALS_DIR" >&2
    exit 2
fi

variant="$1"
port="$2"
trials_dir="$3"
case "$variant" in
    terminus2_baseline|terminus2_evomem|terminus2_self_action|terminus2_open_ended|terminus2_evolution) ;;
    *) echo "unsupported variant: $variant" >&2; exit 2 ;;
esac
case "$port" in
    8040|8041|8042|8043|8044) ;;
    *) echo "unsupported local endpoint port: $port" >&2; exit 2 ;;
esac

project_root="$(cd "$(dirname "$0")/.." && pwd)"
terminal_root="$project_root/benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo"
runner="$terminal_root/harbor_EvoMem/scripts/run_chain.py"
dataset="$terminal_root/Terminal-Bench-Evo"
harbor_bin="$project_root/.venv-terminal/bin/harbor"

source "$project_root/scripts/docker-env.sh"
export PATH="$project_root/.venv-terminal/bin:$PATH"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export HARBOR_EVOMEM_LLM_ENV="$terminal_root/harbor_EvoMem/scripts/terminus2_llm_${port}.env"

max_tokens=2048
agent_timeout=900
if [ "$variant" = "terminus2_evolution" ]; then
    max_tokens=4096
    agent_timeout=1500
fi

exec python "$runner" \
    --dataset "$dataset" \
    --chain fix-git \
    --variant "$variant" \
    --trials-dir "$trials_dir" \
    --no-stop-on-failure \
    --ae GIT_PAGER=cat \
    --ae PAGER=cat \
    --kwarg record_terminal_session=false \
    --kwarg max_turns=15 \
    --kwarg suppress_max_turns_warning=true \
    --kwarg "llm_call_kwargs={\"max_tokens\":${max_tokens}}" \
    --harbor-bin "$harbor_bin" \
    -- --agent-timeout "$agent_timeout" --verifier-timeout 300
