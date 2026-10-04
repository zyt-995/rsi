#!/bin/sh
set -eu
# A local installation of the exact pinned uv release, not a test replacement.
/opt/rsi-verifier/bin/uv --version | grep -Eq '^uv 0\.9\.5( |$)'
mkdir -p "$HOME/.local/bin"
for rsi_binary in uv uvx; do
    cp "/opt/rsi-verifier/bin/$rsi_binary" "$HOME/.local/bin/.$rsi_binary.rsi.$$"
    mv "$HOME/.local/bin/.$rsi_binary.rsi.$$" "$HOME/.local/bin/$rsi_binary"
done
cp /opt/rsi-verifier/env "$HOME/.local/bin/.env.rsi.$$"
mv "$HOME/.local/bin/.env.rsi.$$" "$HOME/.local/bin/env"
echo 'Installed locally cached uv 0.9.5' >&2
