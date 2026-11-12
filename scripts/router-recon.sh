#!/usr/bin/env bash
# Capture read-only command output from an Asus router (Phase 0).
#
#   scripts/router-recon.sh admin@192.168.50.1 [path/to/key]
#   ROUTER_PORT=10022 scripts/router-recon.sh ...   (if SSH isn't on port 22)
#
# Every command below only reads. Nothing is changed or installed.
# Output goes to recon/<timestamp>/ (git-ignored). Look it over before
# sharing: it contains MAC addresses, hostnames and your WAN IP.
set -euo pipefail

target=${1:?usage: $0 user@router-ip [ssh-key]}
key=${2:-secrets/router_key}
out="recon/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$out"

ssh_opts=(-o ConnectTimeout=10 -o BatchMode=yes -p "${ROUTER_PORT:-22}")
[[ -f "$key" ]] && ssh_opts+=(-i "$key")

run() {  # run <name> <command>
  local name=$1; shift
  printf '%-22s' "$name"
  # shellcheck disable=SC2029  # the command is meant to be sent as one string
  if ssh "${ssh_opts[@]}" "$target" "$*" >"$out/$name.txt" 2>&1; then echo ok; else echo "exit $? (kept output)"; fi
}

# Same command text the collector sends, so the output can become test fixtures.
fast='echo @@net_dev; cat /proc/net/dev; echo @@loadavg; cat /proc/loadavg; echo @@meminfo; cat /proc/meminfo; echo @@uptime; cat /proc/uptime; echo @@stat; head -n 1 /proc/stat; echo @@conntrack; cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null; echo @@wan_state; nvram get wan0_state_t; nvram get link_wan'
keys='productid model firmver buildno extendno lan_ipaddr lan_hwaddr wan0_ifname wan0_proto wan0_state_t wan0_ipaddr wan0_gateway wan0_dns link_wan wl_ifnames wl0_ifname wl1_ifname wl2_ifname wl3_ifname cfg_device_list'
slow="echo @@nvram; for k in $keys; do printf '%s=%s\n' \"\$k\" \"\$(nvram get \$k)\"; done; echo @@thermal; for f in /sys/class/thermal/thermal_zone*/temp; do [ -r \"\$f\" ] && echo \"\$f \$(cat \$f)\"; done; echo @@leases; cat /var/lib/misc/dnsmasq.leases 2>/dev/null; echo @@arp; cat /proc/net/arp"

run fast_snapshot "$fast"
run slow_snapshot "$slow"
ifaces=$(ssh "${ssh_opts[@]}" "$target" 'nvram get wl_ifnames')
run wifi_snapshot "for i in $ifaces; do echo \"@@wl_temp \$i\"; wl -i \$i phy_tempsense 2>/dev/null; for m in \$(wl -i \$i assoclist 2>/dev/null | awk '{print \$2}'); do echo \"@@sta \$i \$m\"; wl -i \$i rssi \$m 2>/dev/null; wl -i \$i sta_info \$m 2>/dev/null; done; done"
run ping_busybox 'ping -c 5 -w 7 1.1.1.1 2>&1'
run traceroute 'traceroute -n -m 5 -w 1 -q 1 1.1.1.1 2>&1'

# Extra context to check the collector's assumptions.
run uname 'uname -a; cat /proc/version'
run busybox 'busybox 2>&1 | head -n 40'
run interfaces 'ip -o link 2>/dev/null || ifconfig -a'
run routes 'ip route 2>/dev/null || route -n'
run wl_status "for i in $ifaces; do echo \"== \$i\"; wl -i \$i status 2>&1 | head -n 15; done"

echo
echo "Saved to $out/"
echo "Mesh nodes listed by the router:"
grep '^cfg_device_list=' "$out/slow_snapshot.txt" | tr '<' '\n' | tail -n +2 | sed 's/^/  /'
echo
echo "To check mesh nodes accept the same key, run: ssh -p ${ROUTER_PORT:-22} -i $key ${target%@*}@<node-ip> 'nvram get productid'"
