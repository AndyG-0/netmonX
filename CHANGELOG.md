# Changelog

All notable changes to netmonX are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

Add entries under **Unreleased** as you work. `scripts/release.sh` moves them
under the new version when you cut a release.

## [Unreleased]

### Added
- First public release: read-only SSH collector for stock Asuswrt routers and AiMesh nodes,
  Prometheus, Grafana dashboards and alerts, smokeping, blackbox, Speedtest Tracker and NetAlertX.
- Collector image published to `ghcr.io/andyg-0/netmonx-collector` for amd64 and arm64.
- `netmonx_collector_build_info` metric with the running version.
- GitHub Actions CI, image publishing and release workflows; `scripts/release.sh` for semver releases.
- Collector self-metrics: SSH command duration histogram, command errors, connects and parse failures, with a "Collector" row on the Router & Mesh dashboard.
- Device presence: online gauge, first/last seen and new-device flag, kept across restarts in the `collector_state` volume.
- Roaming history: roams counter per device, the latest roam's from/to node and band, and a Presence & roaming row on the Clients dashboard.
- Vendor label on `asus_client_info`: private (random) MACs always, maker names from an optional OUI file (`scripts/update-oui.sh`).
- `devices.yml`: names, owners, types and per-device offline alerts by MAC.
- Nine more alerts: node rebooted, temperature over 85 °C, connection table over 80%, memory under 10%, WAN IP changed, DHCP pool almost full (new `asus_dhcp_pool_size`), collector data stale, new device, known device offline.
- Per-radio channel, width, DFS flag and noise floor, plus client SNR; Wi-Fi radios row on the Router & Mesh dashboard.
- `asus_wifi_channel_changes_total`: channel or width changes per radio.
- Channel utilization from `wl chanim_stats` (`asus_wifi_airtime_percent`).
- Firmware update check: `asus_firmware_update_available`, a Nodes table column and an alert.
- Dual WAN: per-WAN state and addressing, failover alert.
- IPv6: settings info and `ping6` from the router when IPv6 is on (`ROUTER_PING6_TARGETS`).
- Security exposure: WAN admin access, UPnP, WPS, port forwards and UPnP mappings, with alerts and a Security & VPN dashboard row.
- VPN clients per server (OpenVPN status file, WireGuard handshakes).
- Per-device LAN pings (opt-in `LAN_CLIENT_PING`) through the collector's `/sd/clients` service discovery.
- Blackbox ICMP job for the router, mesh nodes and ISP gateway, discovered from the collector.
- Dashboard annotations for WAN outages, reboots and WAN IP changes.
- Status dashboard for wall displays; optional anonymous viewing with `GRAFANA_ANONYMOUS=true`.
- Config drift: alert when the router's settings change (hash computed on the router).
- Wi-Fi experience score per device, node and network; per-client retry counters.
- Sticky and misplaced (2.4 GHz but 5 GHz capable) client tables.
- Device dashboard: one device's signal, link rate, traffic, attachment history and lease; open it from a MAC on Clients.
- Topology Node Graph on Router & Mesh: router, mesh nodes and the devices on each.
- ISP report card dashboard and 30-day recording rules; ISP_PLAN_DOWN/UP compare speed tests with your plan.
- Bufferbloat grade (A+ to F) from Speedtest Tracker's loaded latency.
- Per-hop traceroute loss and latency (3 probes per hop).
- Router log events (logins, WAN, Wi-Fi, OOM) as counters, with failed-login and out-of-memory alerts.
- `scripts/backup.sh` / `scripts/restore.sh` for the data volumes (Prometheus, Grafana, Speedtest Tracker sqlite, collector state, NetAlertX).
- `scripts/setup.sh` setup wizard: writes .env, makes the router key, checks the login and fills in router/mesh IPs.
- Weekly digest to ntfy (`scripts/digest.py`, `digest` compose profile).
- `iperf3` profile and `scripts/lan-speed.sh` for LAN speed tests, graphed on the Clients dashboard.
- Publish-only MQTT bridge (WAN state, device presence, Home Assistant discovery), off unless `MQTT_URL` is set.
- Ethernet port link speed for every node (`asus_port_*`, from the router's diag file), a Router & Mesh panel and two alerts.

### Changed
- `asus_wan_up`, `asus_wan_link_up` and `asus_wan_info` have a `wan` label ("0", and "1" with dual WAN). The WAN-down alert fires only when every WAN is down.
- The `dns` job finds the router's DNS from `ROUTER_HOST` instead of a hard-coded IP.
- Mesh node names: every series with a `node` label also gets `node_name`, e.g. "Office (192.168.50.147)", from each node's AiMesh location name (`cfg_alias`, read on the main router with `grep`). Dashboards, the Router `$node` picker, alert summaries and the digest show it. Existing series start over once under the new label set.
- Clients: the Online timeline shows 20 devices per page with a pager instead of squeezing them all in, and reads Online/Offline.
- Device dashboard opens on a named device (unnamed ones are listed as "unnamed <MAC>").
- Device ping and LAN speed panels say how to switch them on instead of "No data".
- `./dev.sh` also starts the optional services in `COMPOSE_PROFILES` (iperf3, digest; netalertx only on Linux).

### Fixed
- Alert summaries never rendered: the rules used `$$labels`, which this Grafana doesn't unescape, so every summary failed with "bad character U+0024". They use `$labels` now.
- `./dev.sh`: Prometheus couldn't reach the collector's service discovery URLs, so the DNS, LAN ping and per-device ping jobs had no targets in local development.

[Unreleased]: https://github.com/AndyG-0/netmonX/commits/main
