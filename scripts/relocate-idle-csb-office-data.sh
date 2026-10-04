#!/usr/bin/env bash
# One-time, recoverable relocation of this project's inactive static task data.
# No benchmark contents are edited or discarded. The original path stays valid.
set -euo pipefail
csb_source=/home/ytzhang/rsi-harness/benchmarks/vendor/continual-skill-bench/Continual-Skill-Bench/tasks/office-100-independent-trials
csb_parent=/data2/ytzhang/rsi-harness-static-data
csb_target=/data2/ytzhang/rsi-harness-static-data/csb-office-100-independent-trials
test -d "$csb_source"
test ! -L "$csb_source"
test ! -e "$csb_target"
if lsof -nP +D "$csb_source" 2>/dev/null | rg -q .; then
    echo 'Refusing relocation: data has open handles.' >&2
    exit 1
fi
csb_before=$(cd "$csb_source" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)
mkdir -p "$csb_parent"
mv -- "$csb_source" "$csb_target"
ln -s -- "$csb_target" "$csb_source"
csb_after=$(cd "$csb_source" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)
test "$csb_before" = "$csb_after"
echo "Relocated static CSB data with unchanged aggregate file digest: $csb_after"
df -h /home/ytzhang/rsi-harness /data2
