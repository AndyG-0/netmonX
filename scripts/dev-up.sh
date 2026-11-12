#!/usr/bin/env bash
# Start the stack against a fake router container, for testing without hardware.
# Uses `docker compose` if present, else podman-compose (via uvx if needed).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f secrets/dev_key ]] || ssh-keygen -q -t ed25519 -N '' -C netmonx-dev -f secrets/dev_key
if docker compose version >/dev/null 2>&1; then compose=(docker compose)
elif command -v podman-compose >/dev/null 2>&1 && podman-compose version >/dev/null 2>&1; then compose=(podman-compose)
else compose=(uvx podman-compose); fi
if (($#)); then args=("$@"); else args=(up -d --build); fi
"${compose[@]}" --env-file dev/dev.env -f docker-compose.yml -f dev/docker-compose.dev.yml "${args[@]}"
