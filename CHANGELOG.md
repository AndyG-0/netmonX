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

### Changed
- `asus_wan_up`, `asus_wan_link_up` and `asus_wan_info` have a `wan` label ("0", and "1" with dual WAN). The WAN-down alert fires only when every WAN is down.

[Unreleased]: https://github.com/AndyG-0/netmonX/commits/main
