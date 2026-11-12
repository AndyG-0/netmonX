# netmonX

[![ci](https://github.com/AndyG-0/netmonX/actions/workflows/publish.yml/badge.svg)](https://github.com/AndyG-0/netmonX/actions/workflows/publish.yml)
[![release](https://img.shields.io/github/v/release/AndyG-0/netmonX?sort=semver)](https://github.com/AndyG-0/netmonX/releases)

A home network dashboard for an Asus ZenWiFi / AiMesh network on **stock firmware**. You don't need to flash it. Everything starts with `docker compose up`.

It shows:
- ISP latency, jitter and packet loss, measured from the router itself (over SSH) and from the LAN, so you can tell ISP problems from Wi-Fi or home-network problems
- WAN throughput, WAN up/down history and WAN IP changes
- Speed test history
- DNS lookup time (router vs 1.1.1.1 vs 8.8.8.8) and website reachability
- Health of the router and each mesh node: CPU, memory, temperature, conntrack, uptime and reboots
- Every device on the mesh: which node it's on, wired or Wi-Fi, band, SSID (including guest networks) and signal strength. Also link rates, DHCP leases and new-device alerts (NetAlertX)
- Alerts (Grafana → ntfy or email) for WAN down, packet loss, high latency, an unreachable node and slow speed tests

| Service | What it does | Port |
|---|---|---|
| `asus-collector` (built here, `ghcr.io/andyg-0/netmonx-collector`) | Reads the router over SSH, **read-only** | 9877 (internal) |
| `smokeping` | Pings from the LAN once per second | internal |
| `blackbox` | DNS and HTTPS checks | internal |
| `speedtest-tracker` | Scheduled speed tests, with its own UI | `SPEEDTEST_PORT` (18765) |
| `prometheus` | Stores all metrics (1 year by default) | `PROMETHEUS_PORT` (9090) |
| `grafana` | Dashboards and alerts | `GRAFANA_PORT` (3000) |
| `netalertx` (optional) | Device discovery and new-device alerts | 20211 (host network) |

## What the collector does on your router

Nothing changes. The collector can only send the fixed set of commands in
`collector/netmon_collector/commands.py`: `cat` of `/proc` files and the router's client list (`/tmp/clientlist.json`), `nvram get`, `ping`, `traceroute` and `wl` status queries. Every input (IPs, interface names) is validated before it reaches a command. The collector never runs `nvram set`/`commit`, never writes to `/jffs` and installs nothing. A test enforces this (`test_commands_are_read_only`).

To check this yourself, run `nvram show | md5sum` on the router before and after a day of running. The two hashes should match, apart from counters the firmware updates on its own.

## Setup

### 1. Router SSH (once)

1. In the Asus web UI, go to **Administration → System**:
   - **Enable SSH: LAN only.** Do not choose "LAN & WAN". Exposing SSH on the WAN is a real risk.
   - Keep port 22, or set your own port and put it in `ROUTER_PORT`.
   - Turn off password login if you like. Key login is all this project needs.
2. **On the machine that runs netmonX** (not on the router), in this folder, make a dedicated key pair.
   Then paste the *public* half into **Authorized Keys** in the router's web UI. The private key never leaves this machine:
   ```sh
   ssh-keygen -t ed25519 -N '' -C netmonx -f secrets/router_key
   cat secrets/router_key.pub    # paste this into the router UI, then Apply
   chmod 600 secrets/router_key
   ```
   The SSH username is your router's admin login name.
3. Test it: `ssh -i secrets/router_key admin@192.168.50.1 'nvram get productid'`

### 2. Capture your router's real output (recommended)

The parser tests ship with **sample** output. To check them against your firmware, run:

```sh
scripts/router-recon.sh admin@192.168.50.1     # add ROUTER_PORT=... in front if SSH isn't on port 22
```

The script runs read-only commands and saves the output in `recon/<timestamp>/` (git-ignored). The output contains MACs, hostnames and your WAN IP, so look it over before sharing it. It also lists your mesh nodes and prints a command to check whether they accept the same key.

To use the capture as test fixtures, copy `fast_snapshot.txt`, `slow_snapshot.txt`, `wifi_snapshot.txt`, `ping_busybox.txt` and `traceroute.txt` into `collector/tests/fixtures/`. Then update the expected values in the tests and run:

```sh
cd collector && uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```

### 3. Configure

Get the files: download `netmonx-<version>.tar.gz` from the [latest release](https://github.com/AndyG-0/netmonX/releases/latest) and unpack it, or clone the repo at a release tag (`git clone --branch v0.1.0 https://github.com/AndyG-0/netmonX`). Then:

```sh
cp .env.example .env
```

Edit `.env`:
- `NETMONX_VERSION`: the collector image version. `.env.example` pins the newest release; `latest` follows stable releases and `edge` follows the main branch.
- `HOST_UID` / `HOST_GID`: the output of `id -u` / `id -g`. The collector runs as this user so it can read the key.
- `ROUTER_HOST`, `ROUTER_USER`, `ROUTER_PORT`
- `GRAFANA_ADMIN_PASSWORD`
- `SPEEDTEST_APP_KEY`: generate it with `echo "base64:$(openssl rand -base64 32)"`
- `NETALERTX_URL`, `SPEEDTEST_APP_URL`: the addresses your browser uses to reach this host. They're used for dashboard links.
- `MESH_SSH=false` if your mesh nodes don't accept the key. They'll still be pinged from the LAN.
- Remove `netalertx` from `COMPOSE_PROFILES` if you don't want device discovery.

Next, edit the IPs in two config files:
- `config/smokeping/config.yml`: your router and mesh node IPs. After the collector runs, add the ISP hop it found (shown in the Grafana *ISP* dashboard and as `asus_isp_hop_info`).
- `config/prometheus/prometheus.yml` (job `dns`): your router's IP, if it isn't `192.168.50.1`.

If you changed the URLs in `.env`, rebuild the dashboards:
```sh
python3 scripts/build_dashboards.py
```

### 4. Start

```sh
docker compose pull
docker compose up -d
```

The collector image comes from `ghcr.io/andyg-0/netmonx-collector` (amd64 and arm64). Nothing is built on the server.

- Grafana: `http://<host>:3000`. The *Overview* dashboard is the home page. *ISP*, *Router & mesh* and *Clients* are in the netmonX folder.
- Prometheus targets: `http://<host>:9090/targets`. Every target should be UP. Speedtest stays down until you do step 5.

### 5. Turn on Speedtest Tracker's metrics

Speedtest Tracker serves its Prometheus metrics only after you enable them:
1. Open `http://<host>:18765` and log in. The default login is `admin@example.com` / `password`; change it right away.
2. Go to **Settings → Data Integration**, enable **Prometheus** and save. Leave allowed IPs empty, or add the Docker network's range.
3. Run a test once (**Speedtest → Run**) so the dashboards have a first value.

### 6. Alerts (optional)

Grafana loads 6 alert rules from `config/grafana/provisioning/alerting/rules.yml`. To get notified:

```sh
cp config/grafana/provisioning/alerting/contact-points.yml.example \
   config/grafana/provisioning/alerting/contact-points.yml
# edit the ntfy topic URL, then:
docker compose restart grafana
```

You can also set up email or other contact points in the Grafana UI.

### Upgrading

1. Read the [release notes](https://github.com/AndyG-0/netmonX/releases). Config or dashboard changes show up there.
2. If the release changed files in `config/`, `dashboards/` or `docker-compose.yml`, update them (`git pull` / `git checkout v<version>`, or unpack the new bundle over your copy; `.env` and `secrets/` aren't in the bundle).
3. Set `NETMONX_VERSION=<version>` in `.env`, then run `docker compose pull && docker compose up -d`.
4. Check the version: `netmonx_collector_build_info` in Prometheus shows what's running.

To roll back, set the previous version and run the same commands.

## Notes and caveats

- **ISP hop:** with `ISP_HOP=auto`, the collector pings your WAN gateway. If that gateway is a private IP (the modem is in router mode), it pings the first ISP-side hop from traceroute instead. Some ISPs drop ICMP to their gateway, which looks like 100% loss. In that case, set `ISP_HOP=none` or pick a hop that answers.
- **Mesh nodes** are found from the main router's `cfg_device_list`. Set `MESH_NODES` to a list of IPs to override this, or to `none`.
- **Radio temperatures** come from `wl phy_tempsense`. Some models report them on a different scale. Compare against the router UI.
- **Clients:** device counts, the node each device is on, and wired vs. Wi-Fi come from the main router's AiMesh client list, so they cover every node. The main router also lists devices behind a mesh node as wired; the collector counts each device once, on the node it's actually attached to. Link rate and traffic come from `wl` on each node and need `MESH_SSH=true`.
- **Device names** come from the names you set in the Asus app, else from DHCP. Name your mesh nodes differently in the app (all four are often named the same) to see names instead of IPs in the *Devices by mesh node* panels.
- **`cryptography` is pinned to 46.0.7** in the collector. Newer wheels crashed with "Illegal instruction" on some ARM virtual CPUs (podman on Apple Silicon). On x86 Linux you can probably unpin it.
- The collector keeps one SSH session per node and reconnects on failure. If a poll fails, its values are dropped rather than left stale, and `asus_up` goes to 0.

## Local development (macOS or Linux)

`./dev.sh` runs the collector **on your machine** and restarts it each time you save a file in `collector/netmon_collector/`. Prometheus, Grafana, smokeping, blackbox and Speedtest Tracker run in podman or Docker. Prometheus scrapes the collector through the engine's host alias (`host.containers.internal`, or `host.docker.internal` for Docker). The collector listens on 127.0.0.1 only.

```sh
./dev.sh             # containers + collector against the real router (settings from .env, key secrets/router_key)
./dev.sh fake        # same, against a fake router container (no hardware needed)
./dev.sh collector   # only the collector (add "fake" for the fake router)
./dev.sh containers  # restart only the containers (after changing .env or dashboards)
FAST=1 ./dev.sh      # poll every 5 s / 30 s and scrape every 5 s, for quick testing
./dev.sh check       # ruff, tests, dashboards up to date, shellcheck, actionlint, compose config (CI runs this too)
./dev.sh recon       # read-only router capture into recon/
./dev.sh logs grafana | ps | stop | down
```

Notes:
- Grafana login: `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` from `.env`. On each start, `dev.sh` resets the admin password to match `.env`, because Grafana only reads that variable on its first start.
- If a host port is taken, `dev.sh` adds 10000 to it for that run and prints the new port.
- Local Grafana uses its own copy of the dashboards (`dev/.dashboards`, git-ignored). There, the Speedtest link points at localhost. NetAlertX isn't started locally: it needs host networking for ARP scans, which doesn't work in the podman VM, so it only runs on the Linux server.
- `FAST=1` is for testing only. On the router it means a few more SSH commands per minute; they're still read-only.
- If `SPEEDTEST_APP_KEY` is empty, `dev.sh` generates it and saves it to `.env`.
- The dev containers use their own compose project, `netmonx-dev`, so their data is kept apart from a full deployment.

`scripts/dev-up.sh` is still there for testing the **collector image** itself; it builds the image from `collector/` instead of pulling it. It runs the full compose stack, collector included, against the fake router.

The fake router is an Alpine container with dropbear and stubbed `nvram` and `wl` commands. Its traceroute shows only one hop, and it has no `br0`, so "Devices online" stays empty there.

## CI and releases

GitHub Actions (`.github/workflows/`):
- **`ci.yml`** runs on every pull request: `./dev.sh check`, an image build with a smoke test and a Trivy scan (fixable HIGH/CRITICAL issues fail it), an end-to-end run against the fake router, and a gitleaks secret scan.
- **`publish.yml`** runs on pushes to `main` and on version tags. It runs the CI jobs first, then builds the image natively for amd64 and arm64 and pushes it to ghcr.io:

  | Trigger | Image tags | Also |
  |---|---|---|
  | push to `main` | `edge`, `sha-<short>` | |
  | tag `v1.2.3` | `1.2.3`, `1.2`, `1` (from 1.0.0 on), `latest` | GitHub Release with notes and a deploy bundle |
  | tag `v1.3.0-rc.1` | `1.3.0-rc.1` | GitHub prerelease |

Dependabot opens weekly grouped PRs for actions, Python, Docker and compose image updates.

### Cutting a release

Versions follow [semver](https://semver.org). The version lives in `collector/pyproject.toml`; tags are `vX.Y.Z`.

1. As you work, add notes under `## [Unreleased]` in `CHANGELOG.md`.
2. On an up-to-date, clean `main` with CI green, run:
   ```sh
   ./dev.sh release patch          # 0.1.0 -> 0.1.1   (also: minor, major, or an explicit 1.2.3)
   ./dev.sh release minor --pre rc # 0.1.1 -> 0.2.0-rc.1
   ./dev.sh release pre            # 0.2.0-rc.1 -> 0.2.0-rc.2
   ./dev.sh release --finalize     # 0.2.0-rc.2 -> 0.2.0
   ./dev.sh release patch --dry-run   # show what would change, change nothing
   ```
   The script checks the branch and tree, runs `./dev.sh check`, bumps the version (and `NETMONX_VERSION` in `.env.example` for stable releases), moves the Unreleased notes under the new version, shows the diff, and asks before it commits, tags and pushes.
3. The tag starts `publish.yml`. It fails before pushing anything if the tag and `pyproject.toml` disagree. The GitHub Release gets the CHANGELOG section, GitHub's list of merged PRs and `netmonx-<version>.tar.gz`.

Prereleases don't touch `CHANGELOG.md`; their notes come from Unreleased, and the final release moves them under its version.

## Layout

```
collector/            asus-collector image (Python 3.12, asyncssh, prometheus-client)
config/               prometheus, smokeping, blackbox, grafana provisioning + alert rules
dashboards/           generated Grafana JSON (from scripts/build_dashboards.py)
scripts/              release.sh, changelog.sh, router-recon.sh, build_dashboards.py, dev-up.sh
.github/              CI, publish and release workflows, Dependabot
dev.sh                local dev runner (collector on host, rest in containers)
dev/                  fake router + dev overrides
secrets/              router_key (git-ignored)
```
