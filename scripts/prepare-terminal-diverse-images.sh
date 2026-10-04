#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
normalize="$project_root/scripts/normalize-image-for-singleuid.sh"

images=(
    docker.m.daocloud.io/library/ubuntu:22.04\|ubuntu:22.04
    docker.m.daocloud.io/library/python:3.13-slim-bookworm\|python:3.13-slim-bookworm
)

for pair in "${images[@]}"; do
    source_image="${pair%%|*}"
    target_image="${pair#*|}"
    "$normalize" "$source_image" "$target_image"
done
