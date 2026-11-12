#!/usr/bin/env bash
# LAN speed test against the iperf3 server (COMPOSE_PROFILES=iperf3), from a laptop or desktop.
#
#   scripts/lan-speed.sh <netmonx-host> [name]     e.g. scripts/lan-speed.sh 192.168.50.10 "office laptop"
#
# Runs 10 s upload (this machine → server) and 10 s download (server → this machine) over TCP, prints the
# results and pushes them to the pushgateway on the same host, so they show up on the Clients dashboard.
# The name defaults to this machine's hostname. Needs iperf3 and python3 (for the JSON). Nothing is sent
# besides the two speeds and the name. PUSHGATEWAY_URL=none skips the push.
set -euo pipefail

server=${1:?usage: $0 <netmonx-host> [name]}
name=${2:-$(hostname -s 2>/dev/null || hostname)}
port=${IPERF3_PORT:-5201}
push=${PUSHGATEWAY_URL:-http://$server:${PUSHGATEWAY_PORT:-9091}}
secs=${LAN_SPEED_SECONDS:-10}
command -v iperf3 >/dev/null || { echo "lan-speed.sh: install iperf3 first (brew/apt install iperf3)" >&2; exit 1; }

bits() { # bits [iperf3 args...]: received bits/s of one TCP test
  iperf3 -c "$server" -p "$port" -t "$secs" -J "$@" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(d["error"]) if "error" in d else print(round(d["end"]["sum_received"]["bits_per_second"]))'
}
up=$(bits)
down=$(bits -R)
printf '%s → %s: upload %.0f Mbit/s, download %.0f Mbit/s\n' "$name" "$server" \
  "$(bc -l <<<"$up/1000000")" "$(bc -l <<<"$down/1000000")"

[[ $push == none ]] && exit 0
# Pushgateway path segments: base64url-encode the name so spaces and slashes are fine.
enc=$(printf '%s' "$name" | base64 | tr '+/' '-_' | tr -d '=\n')
if curl -fsS --max-time 10 --data-binary @- "$push/metrics/job/lan_speed/client@base64/$enc" <<METRICS
# TYPE netmonx_lan_speed_bits gauge
# HELP netmonx_lan_speed_bits Last LAN speed test from scripts/lan-speed.sh, bits per second.
netmonx_lan_speed_bits{direction="upload"} $up
netmonx_lan_speed_bits{direction="download"} $down
# TYPE netmonx_lan_speed_timestamp_seconds gauge
netmonx_lan_speed_timestamp_seconds $(date +%s)
METRICS
then echo "pushed to $push"; else echo "lan-speed.sh: could not push to $push (pushgateway running?)" >&2; fi
