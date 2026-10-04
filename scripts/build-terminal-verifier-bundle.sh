#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cache="$root/.cache/verifier"
output="$root/.cache/tmux/verifier-runtime-singleuid-amd64.tar.gz"
test -x "$cache/uv-0.9.5/bin/uv"
test -d "$cache/python/cpython-3.13.9-linux-x86_64-gnu"
test -d "$cache/uv-cache"
work="$(mktemp -d "$cache/bundle.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
install -d "$work/opt/rsi-verifier/bin" "$work/opt/rsi-verifier/bootstrap"
install -m 0755 "$cache/uv-0.9.5/bin/uv" "$cache/uv-0.9.5/bin/uvx" "$work/opt/rsi-verifier/bin/"
cp -a "$cache/python" "$cache/uv-cache" "$work/opt/rsi-verifier/"
install -m 0755 "$root/docker/verifier-runtime/curl" "$work/opt/rsi-verifier/bootstrap/curl"
install -m 0644 "$root/docker/verifier-runtime/env" "$root/docker/verifier-runtime/install-uv-0.9.5.sh" "$work/opt/rsi-verifier/"
# The small package-manager shim and CA roots are also installed at verification
# time, including for old/prebuilt images which already contain tmux.
install -d "$work/usr/local/lib" "$work/etc/ssl/certs"
tar -xzOf "$root/.cache/tmux/tmux-runtime-singleuid-amd64.tar.gz" ./usr/local/lib/rsi-singleuid-compat.so > "$work/usr/local/lib/rsi-singleuid-compat.so"
cp -a "$root/docker/singleuid-runtime/usr" "$work/"
install -m 0644 /etc/ssl/certs/ca-certificates.crt "$work/etc/ssl/certs/"
tar --owner=0 --group=0 -C "$work" -czf "$output.tmp" .
mv "$output.tmp" "$output"
sha256sum "$output"
