#!/usr/bin/env bash
# Recoverable, checksum-verified relocation of two inactive project datasets.
set -euo pipefail
command -v lsof >/dev/null
for csb_name in finance-econ-100-independent-trials law-100-independent-trials; do
    csb_source="/home/ytzhang/rsi-harness/benchmarks/vendor/continual-skill-bench/Continual-Skill-Bench/tasks/$csb_name"
    csb_target="/data2/ytzhang/rsi-harness-static-data/csb-$csb_name"
    test -d "$csb_source"
    test ! -L "$csb_source"
    test ! -e "$csb_target"
    if lsof -nP +D "$csb_source" 2>/dev/null | rg -q .; then
        echo "Refusing relocation: $csb_name has open handles." >&2
        exit 1
    fi
    csb_before=$(cd "$csb_source" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)
    mv -- "$csb_source" "$csb_target"
    ln -s -- "$csb_target" "$csb_source"
    csb_after=$(cd "$csb_source" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)
    test "$csb_before" = "$csb_after"
    echo "Relocated $csb_name; unchanged aggregate digest: $csb_after"
done
df -h /home/ytzhang/rsi-harness /data2
