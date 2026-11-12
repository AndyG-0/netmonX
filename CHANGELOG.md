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

[Unreleased]: https://github.com/AndyG-0/netmonX/commits/main
