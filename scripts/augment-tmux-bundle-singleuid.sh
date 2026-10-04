#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
input="${1:-$project_root/.cache/tmux/tmux-runtime-jammy-amd64.tar.gz}"
output="${2:-$project_root/.cache/tmux/tmux-runtime-singleuid-amd64.tar.gz}"
case "$output" in
    "$project_root"/.cache/tmux/*.tar.gz) ;;
    *) echo "output must be below $project_root/.cache/tmux" >&2; exit 2 ;;
esac

work="$(mktemp -d "$project_root/.cache/tmux/augment.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
tar --no-same-owner -xzf "$input" -C "$work"
install -d "$work/usr/local/lib" "$work/usr/local/bin"
if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    install -d "$work/etc/ssl/certs"
    install -m 0644 /etc/ssl/certs/ca-certificates.crt "$work/etc/ssl/certs/ca-certificates.crt"
fi
gcc -shared -fPIC -O2 -Wall -Wextra \
    -o "$work/usr/local/lib/rsi-singleuid-compat.so" \
    "$project_root/docker/singleuid_compat.c" -Wl,--no-as-needed -l:libdl.so.2
install -m 0755 "$project_root/docker/singleuid-runtime/usr/local/bin/apt-get" "$work/usr/local/bin/apt-get"
install -m 0755 "$project_root/docker/singleuid-runtime/usr/local/bin/apt" "$work/usr/local/bin/apt"
install -m 0755 "$project_root/docker/singleuid-runtime/usr/local/bin/dpkg" "$work/usr/local/bin/dpkg"
tar -C "$work" -czf "$output.tmp" .
mv "$output.tmp" "$output"
sha256sum "$output"
