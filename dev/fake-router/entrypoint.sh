#!/bin/sh
set -e
install -d -m 700 /root/.ssh
install -m 600 /keys/authorized_keys /root/.ssh/authorized_keys
cat > /var/lib/misc/dnsmasq.leases <<LEASES
86213 7c:d9:5c:11:22:33 192.168.50.23 Pixel-8 01:7c:d9:5c:11:22:33
85120 a4:83:e7:44:55:66 192.168.50.41 * *
LEASES
chmod 644 /var/lib/misc/dnsmasq.leases
# AiMesh client list: main router (…:01) and one mesh node (…:02). The NAS shows twice, like on a real
# router: wired on the main router (seen over the backhaul) and Wi-Fi on the node.
cat > /tmp/clientlist.json <<'JSON'
{"02:00:00:AA:BB:01":{"2G":{"3C:22:FB:00:11:22":{"ip":"192.168.50.60","rssi":"-58"}},"2G_1":{"5E:11:22:33:44:55":{"ip":"192.168.101.20","rssi":"-66"}},"5G":{"7C:D9:5C:11:22:33":{"ip":"192.168.50.23","rssi":"-49"},"A4:83:E7:44:55:66":{"ip":"192.168.50.41","rssi":"-71"}},"wired_mac":{"00:11:32:AA:00:01":{"ip":"192.168.50.10"},"02:00:00:AA:BB:02":{"ip":"192.168.50.2"},"D0:50:99:00:00:09":{"ip":"192.168.50.70"}}},
 "02:00:00:AA:BB:02":{"5G":{"D0:50:99:00:00:09":{"ip":"192.168.50.70","rssi":"-62"}},"wired_mac":{"B8:27:EB:00:00:05":{"ip":"192.168.50.80"}}}}
JSON
# One UPnP mapping (miniupnpd lease format).
echo "UDP:3074:192.168.50.60:3074:0:Game console" > /tmp/upnp.leases
# A few router log lines (the collector counts only lines added after its first poll).
cat > /tmp/syslog.log <<'LOG'
Nov 11 22:40:12 wlceventd: wlceventd_proc_event(722): eth6: Assoc 7C:D9:5C:11:22:33, status: Successful (0), rssi:-49
Nov 11 22:41:30 roamast: Roam a client [D0:50:99:00:00:09], status [0]
LOG
# Ethernet ports (the web UI's diag file): main router WAN at 2.5G, the node's uplink at 1G, one LAN port at 100M.
cat > /tmp/diag_port_status.json <<'JSON'
{"port_info":{"02:00:00:AA:BB:01":{"W0":{"is_on":"1","max_rate":"2500","link_rate":"2500"},"L1":{"is_on":"1","max_rate":"1000","link_rate":"1000"},"L2":{"is_on":"0","max_rate":"1000","link_rate":"0"}},
 "02:00:00:AA:BB:02":{"W0":{"is_on":"1","max_rate":"2500","link_rate":"1000"},"L1":{"is_on":"1","max_rate":"1000","link_rate":"100"}}}}
JSON
# The main router's copy of a node's settings; the collector reads only cfg_alias (the node's name in the app).
echo '{"wireless":{"wl0_ssid":"HomeNet"},"misc":{"cfg_alias":"Office"}}' > '/tmp/02:00:00:AA:BB:02.json'
# -s: no password logins, -R: create host keys, -F/-E: foreground, log to stderr
exec dropbear -F -E -R -s -p 22
