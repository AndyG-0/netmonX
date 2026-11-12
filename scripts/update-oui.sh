#!/usr/bin/env bash
# Download Wireshark's MAC vendor table so the collector can label devices with their maker.
#
#   scripts/update-oui.sh            writes config/collector/manuf (git-ignored)
#
# The collector reads it at startup (OUI_FILE); restart it after updating:
#   docker compose restart asus-collector
# Devices using a random "private" MAC show as "Private (random) MAC" with or without this file.
set -euo pipefail
cd "$(dirname "$0")/.."
url=${OUI_URL:-https://www.wireshark.org/download/automated/data/manuf}
out=config/collector/manuf
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 "$url" -o "$tmp"
# Sanity check: a real table has tens of thousands of prefixes.
lines=$(grep -c $'^[0-9A-Fa-f][0-9A-Fa-f]:' "$tmp" || true)
(( lines > 10000 )) || { echo "update-oui.sh: $url doesn't look like a manuf file ($lines prefixes)" >&2; exit 1; }
mv "$tmp" "$out"
trap - EXIT
echo "wrote $out ($lines prefixes)"
