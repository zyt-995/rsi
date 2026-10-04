#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
source "$project_root/scripts/docker-env.sh"
image="${1:?usage: prepare-singleuid-base.sh IMAGE}"
if [ "$(docker image inspect "$image" --format '{{index .Config.Labels "rsi.singleuid_base_version"}}')" = 5 ]; then
    exit 0
fi
container="$(docker create --network host "$image" sleep infinity)"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
docker start "$container" >/dev/null
# Reuse the existing package-manager-only compatibility wrappers. Do not
# preload these for model commands or tests.
tar -xzOf "$project_root/.cache/tmux/tmux-runtime-singleuid-amd64.tar.gz" \
    ./usr/local/lib/rsi-singleuid-compat.so \
    | docker exec -i "$container" sh -c 'mkdir -p /usr/local/lib; dd of=/usr/local/lib/rsi-singleuid-compat.so status=none'
tar --owner=0 --group=0 -C "$project_root/docker/singleuid-runtime" -cf - usr \
    | docker exec -i "$container" tar --no-same-owner -xf - -C /
# Ubuntu minimal bases have no CA bundle. Install the host's trusted public
# roots before the apt wrappers switch HTTP repository URLs to HTTPS.
tar --owner=0 --group=0 -C / -cf - etc/ssl/certs/ca-certificates.crt \
    | docker exec -i "$container" tar --no-same-owner -xf - -C /
docker exec "$container" sh -c '
    mkdir -p /etc/apt/apt.conf.d
    printf "APT::Sandbox::User \"root\";\nAcquire::http::Timeout \"30\";\nAcquire::https::Timeout \"30\";\nAcquire::Retries \"2\";\n" > /etc/apt/apt.conf.d/99-rsi-singleuid
    chmod +x /usr/local/bin/apt /usr/local/bin/apt-get /usr/local/bin/dpkg
'
safe="${image//\//_}"
safe="${safe//:/_}"
docker tag "$image" "rsi-backup/base-before-v5-${safe}:latest"
docker commit --change 'LABEL rsi.single_uid_normalized=true' \
    --change 'LABEL rsi.singleuid_base_version=5' "$container" "$image" >/dev/null
echo "base compatible: $image"
