# netmonX TODO

Ideas for future work, grouped by priority. Inspiration in [brackets].
Rule for everything here: router access stays read-only (`collector/netmon_collector/commands.py`).
Effort: S = an evening, M = a weekend, L = bigger.

## Questions for Andy

- **OUI dataset:** vendor names need Wireshark's `manuf` file (~2 MB, GPL-2 distribution). I didn't commit it; `scripts/update-oui.sh` fetches it. Commit it (or bake it into the image) if you want vendors out of the box.

## P1: high value, uses data we already collect or can read cheaply

### Clients
- [x] **Roaming history** (S) [UniFi client timeline]: watch for a device's node/band changing between
      clientlist.json polls. Export `asus_client_roams_total{mac,from_node,to_node}`. Add a "Recent roams" panel.
      Done: `asus_client_roams_total{mac}` plus `asus_client_last_roam_timestamp_seconds{from_node,to_node,from_band,to_band}` (a from/to label per roam counter would grow without limit). "Presence & roaming" row on the Clients dashboard.
- [x] **Device first-seen / last-seen / online gauge** (S) [UniFi, eero]: `asus_client_online{mac}` (0/1) and
      `asus_client_last_seen_timestamp_seconds`. Gives presence ("who's home") and a per-device uptime timeline.
      Done: `asus_client_online`, `asus_client_last_seen_timestamp_seconds`, `asus_client_first_seen_timestamp_seconds`, `asus_client_new` (collector/netmon_collector/presence.py). State is saved to `STATE_DIR` (compose volume `collector_state`); devices age out after `CLIENT_RETENTION_DAYS`.
- [x] **Vendor lookup by MAC OUI** (S) [UniFi fingerprinting, Fing]: bundle an offline OUI table
      (IEEE / Wireshark `manuf`) and add a `vendor` label to `asus_client_info`. Show "random/private MAC"
      when the locally-administered bit is set.
      Done: Private-MAC detection always on; maker names from an optional Wireshark `manuf` file (`OUI_FILE`, fetched by `scripts/update-oui.sh`). The dataset isn't committed: see Questions for Andy.
- [x] **Device names / owners / groups file** (S) [UniFi aliases, Firewalla groups]: optional
      `config/devices.yml` mapping MAC → name, owner, type (phone, IoT, PC). Exported as labels so panels can be
      filtered by owner or device type.
      Done: `config/collector/devices.yml` (`DEVICES_FILE`), reloaded on change; adds `owner`/`type` labels and `asus_device_offline_alert_hours`.
- [ ] **Per-device drill-down dashboard** (M) [UniFi client page]: one `$mac` variable. Show signal, link rate,
      node/roam history, traffic, online timeline, DHCP lease and vendor.
- [ ] **Wi-Fi experience score** (M) [UniFi "Wi-Fi Experience", Meraki]: a 0-100 score per client from RSSI,
      tx/rx rate vs. the band's max, and retries (from `wl sta_info`). Show the per-node and network
      averages on Overview.
- [ ] **Sticky / misplaced clients** (S) [UniFi insights]: list Wi-Fi clients that stay below -75 dBm for a
      long time without roaming, and 5 GHz-capable clients sitting on 2.4 GHz.

### Radio / RF
- [ ] **Channel, width and noise per radio** (S) [UniFi RF environment, WiFiman]: `wl -i X chanspec`,
      `wl -i X noise`. Export `asus_wifi_channel_info`, `asus_wifi_noise_dbm`, and SNR per client
      (RSSI minus noise).
- [ ] **Channel utilization / interference** (M) [UniFi channel utilization]: `wl -i X chanim_stats` (busy %,
      interference %, glitches). Check it on the real firmware with `router-recon.sh` first.
- [ ] **Neighbouring networks** (M) [UniFi RF scan, WiFi Analyzer]: read cached `wl -i X scanresults` only, never
      `wl scan`. Count APs per channel to explain interference. Skip if the cache is empty on stock firmware.
- [ ] **DFS / channel-change events** (S): count changes in the exported chanspec; annotate the dashboards.

### Mesh
- [ ] **Backhaul link quality** (M) [UniFi uplink / mesh view, eero]: RSSI, rate and band of each node's
      wireless backhaul (or wired backhaul speed). Source: `cfg_device_list` plus the backhaul STA in
      `wl assoclist`/`sta_info` on the parent. Alert when backhaul drops to 2.4 GHz or below a rate floor.
- [ ] **Topology map** (M) [UniFi topology]: Grafana Node Graph panel showing router → nodes → clients,
      built from the existing `asus_mesh_node_info` and `asus_client_info`.

### Router / WAN
- [ ] **Firmware update available** (S) [UniFi, eero]: read `nvram get webs_state_update webs_state_info`.
      Gauge plus alert.
- [ ] **LAN/WAN port link speed & duplex** (M) [UniFi port view]: read-only `ethctl <if> media-type` /
      `robocfg show` (depends on the model; check with recon first). Alert when a wired backhaul or a
      known PC negotiates at 100 Mb.
- [ ] **Dual-WAN / failover** (S): also read `wan1_*` nvram keys and add a `wan` label.
- [ ] **IPv6** (M): `ipv6_service`, `ipv6_rtr_addr`, `ipv6_prefix` nvram; `ping6` targets from the router.
      Check IPv6 reachability on the ISP dashboard.
- [ ] **Config drift detection** (S) [Oxidized/RANCID]: hash `nvram show` with counters and timestamps left
      out, export `asus_config_hash_info`, and alert when it changes. Builds on the README's md5sum tip.
- [ ] **Security exposure view** (M) [Firewalla, UniFi threat panel]: list port forwards (`vts_rulelist`),
      UPnP mappings (`/tmp/upnp.leases` or similar), WAN SSH/web access flags (`sshd_wan`, `misc_http_x`),
      and whether WPS is on. Alert if any of these become active.
- [ ] **VPN server sessions** (S): connected WireGuard/OpenVPN clients via `wg show` / OpenVPN status files,
      where present.

### Alerts (add to config/grafana/provisioning/alerting/rules.yml)
- [ ] Node rebooted (uptime went down) (S)
- [ ] Temperature above threshold (S)
- [ ] Conntrack > 80% of max; memory available < 10% (S)
- [ ] WAN IP changed (S)
- [ ] DHCP pool almost full (leases vs. `dhcp_start`/`dhcp_end`) (S)
- [ ] Collector data stale (`time() - asus_last_poll_timestamp_seconds`) (S)
- [ ] New device seen (collector-side, so it works without NetAlertX) (S)
- [ ] Known device offline for N hours (opt-in per device via devices.yml) (S)

## P2: monitoring depth

- [ ] **ISP report card / SLA** (M) [eero, Ookla, UniFi Internet health]: Prometheus recording rules for
      monthly uptime %, outage count and total minutes, p95 latency, median speed vs. the plan speed
      (`ISP_PLAN_DOWN/UP` in .env). Add a printable "evidence for your ISP" dashboard.
- [ ] **Outage annotations** (S): Grafana annotations for WAN down/up, node reboots and WAN IP changes.
- [ ] **Bufferbloat grade** (M) [Waveform, Ookla loaded latency]: compare idle latency with the loaded latency
      that speedtest-tracker reports, and grade A-F. Optionally run a ping series during the speed test.
- [ ] **Per-hop path monitoring** (M) [PingPlotter, MTR]: run a traceroute every slow interval and keep loss and
      latency per hop for the first few hops. This shows which hop starts losing packets.
- [ ] **Per-client LAN latency** (M) [UniFi client latency]: the collector serves an HTTP service-discovery
      endpoint (`/sd/clients`, `/sd/nodes`) and Prometheus probes clients with blackbox ICMP. Opt-in, rate-limited.
- [ ] **Auto-discovered ping targets** (S): the same `/sd/nodes` endpoint fills mesh nodes and the ISP hop
      into a blackbox ICMP job. No more hand-editing `config/smokeping/config.yml`.
- [ ] **Templated router IP** (S): the `dns` job in `prometheus.yml` hard-codes 192.168.50.1. Generate it
      from `ROUTER_HOST`, or use the SD endpoint.
- [ ] **Syslog events** (M) [UniFi event log]: `tail -n` of `/tmp/syslog.log` since the last offset. Count
      matched events (WAN reconnect, DFS radar, auth failure, OOM, roam) as counters. Optionally send raw lines
      to Loki in an extra compose profile.
- [ ] **Per-client traffic for wired devices** (L) [UniFi traffic stats, Firewalla]: today's per-client bytes
      come only from `wl` (Wi-Fi). Look at read-only sources: conntrack acct (`/proc/net/nf_conntrack`, when
      `nf_conntrack_acct=1`), or Asus Traffic Analyzer's database (when the user has it on). Research first.
- [ ] **App / category usage** (L) [UniFi DPI, Asus AiProtection]: only if the bwdpi data can be read without
      changes. Research only.
- [ ] **LAN throughput test** (M) [WiFiman]: optional `iperf3 -s` container. Document running a client from
      phones and laptops, and graph the results (or push them via Pushgateway).
- [ ] **DNS filtering integration** (S) [Pi-hole, AdGuard]: optional compose profile with an exporter for
      Pi-hole/AdGuard Home if the user runs one: queries, block %, top clients.
- [x] **Collector self-metrics** (S): per-command SSH duration histogram, reconnect counter, and output parse
      failures.
      Done: `netmonx_collector_ssh_command_duration_seconds`, `_ssh_command_errors_total`, `_ssh_connects_total`, `_parse_failures_total`; "Collector" row on Router & Mesh.

## P3: usability & operations

- [ ] **Weekly digest** (M) [UniFi/eero weekly report]: a small script or cron container that renders key stats
      (uptime, outages, speed, new devices) and sends them to ntfy or email.
- [ ] **Home Assistant / MQTT bridge** (M): publish presence and WAN state over MQTT for automations.
- [ ] **Kiosk / read-only view** (S): Grafana anonymous Viewer role for a wall display, plus a compact
      "status" dashboard.
- [ ] **Backup/restore script** (S): back up the Grafana and Prometheus volumes and speedtest-tracker's sqlite.
- [ ] **Long-term downsampling** (M): optional VictoriaMetrics in place of Prometheus, for years of data at low
      disk use.
- [ ] **Setup wizard** (M): `scripts/setup.sh` writes `.env`, makes the key, and fills in IPs from recon output.
- [x] **Repo hygiene** (S): `git init` and a CI workflow running `./dev.sh check`. Publish a multi-arch collector
      image (arm64 for a Pi/NAS). Done: `.github/workflows/`, `scripts/release.sh`.
- [ ] **Support more models** (M): Asuswrt-Merlin firmware (more `nvram` keys, still no `/jffs` scripts); test
      fixtures from other ZenWiFi/RT models.

## Won't do (breaks the read-only rule or is out of scope)
- Blocking or pausing clients, parental controls, Wake-on-LAN, rebooting nodes, changing channels [UniFi/eero
  controls]. All of these need writes on the router.
- Triggering active Wi-Fi scans (`wl scan`): it disrupts clients. Use cached `scanresults` only.
- Installing agents or scripts on the router (`/jffs`, entware).
