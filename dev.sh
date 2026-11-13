#!/usr/bin/env bash
# Local development: the collector runs on this machine with auto-reload;
# Prometheus, Grafana, smokeping, blackbox and Speedtest Tracker run in containers, plus the optional
# services named in COMPOSE_PROFILES (iperf3, digest; netalertx on Linux only).
#
#   ./dev.sh                 start containers, then run the collector against the real router (.env)
#   ./dev.sh fake            same, against a fake router container (no hardware needed)
#   ./dev.sh collector       run only the collector (real router); add "fake" for the fake one
#   ./dev.sh containers      (re)start only the containers, e.g. after changing .env or dashboards
#   ./dev.sh check           lint, format check, tests, dashboard and compose checks (same as CI)
#   ./dev.sh recon           capture read-only router output into recon/
#   ./dev.sh release patch   cut a release (see scripts/release.sh --help)
#   ./dev.sh ps | logs [svc] | stop | down
#
#   FAST=1 ./dev.sh          poll every 5 s / 30 s and scrape every 5 s, for quick testing
#
# Settings come from .env. The collector reloads when files in collector/netmon_collector change.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD
PROJECT=netmonx-dev
SERVICES=(prometheus grafana smokeping blackbox speedtest-tracker)

die() { echo "dev.sh: $*" >&2; exit 1; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# Read .env line by line (not `source`: values like "17 */2 * * *" aren't shell-safe).
load_env() {
  [[ -f .env ]] || { cp .env.example .env; info "created .env from .env.example"; }
  local line key val
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key=${BASH_REMATCH[1]} val=${BASH_REMATCH[2]}
    val=${val%\"}; val=${val#\"}; val=${val%\'}; val=${val#\'}
    [[ -n ${!key+x} ]] || export "$key=$val"  # values already in the shell win
  done < .env
  # Speedtest Tracker needs a fixed key; make one once and keep it in .env.
  if [[ -z ${SPEEDTEST_APP_KEY:-} ]]; then
    SPEEDTEST_APP_KEY="base64:$(openssl rand -base64 32)"
    if grep -q '^SPEEDTEST_APP_KEY=' .env; then
      sed -i.bak "s|^SPEEDTEST_APP_KEY=.*|SPEEDTEST_APP_KEY=$SPEEDTEST_APP_KEY|" .env && rm -f .env.bak
    else
      echo "SPEEDTEST_APP_KEY=$SPEEDTEST_APP_KEY" >> .env
    fi
    export SPEEDTEST_APP_KEY
    info "generated SPEEDTEST_APP_KEY in .env"
  fi
}

pick_engine() {
  if docker compose version >/dev/null 2>&1; then compose=(docker compose); engine=docker
  elif command -v podman-compose >/dev/null 2>&1 && podman-compose version >/dev/null 2>&1; then compose=(podman-compose); engine=podman
  elif command -v podman >/dev/null 2>&1 && command -v uvx >/dev/null 2>&1; then compose=(uvx podman-compose); engine=podman
  else die "need docker compose, or podman + podman-compose (or uv)"; fi
  command -v podman >/dev/null 2>&1 && [[ $engine == docker ]] && docker --version 2>/dev/null | grep -qi podman && engine=podman
  compose_base=("${compose[@]}")
  compose+=(-p "$PROJECT" -f docker-compose.yml -f dev/docker-compose.local.yml)
}

venv() {
  command -v uv >/dev/null 2>&1 || die "install uv: https://docs.astral.sh/uv/"
  local py=collector/.venv/bin/python stamp=collector/.venv/.installed
  [[ -x $py ]] || uv venv -q --python 3.12 collector/.venv
  if [[ ! -f $stamp || collector/pyproject.toml -nt $stamp ]]; then
    info "installing collector dependencies"
    (cd collector && VIRTUAL_ENV=.venv uv pip install -q -e '.[dev]') && touch "$stamp"
  fi
}

dev_key() { [[ -f secrets/dev_key ]] || ssh-keygen -q -t ed25519 -N '' -C netmonx-dev -f secrets/dev_key; }

# Use another host port if one is taken by another app. Sets the variable.
free_port() {  # free_port VAR default
  local var=$1 port=${!1:-$2}
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 \
     && ! $engine ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Ports}}' | grep -q ":$port->"; then
    info "port $port is in use; $var=$((port + 10000)) for this run"
    port=$((port + 10000))
  fi
  export "$var=$port"
}

# Prometheus in a container scrapes the collector on this machine. FAST=1 scrapes every 5 s.
prometheus_config() {
  local host=host.containers.internal fast=()
  [[ $engine == docker ]] && host=host.docker.internal
  [[ ${FAST:-} == 1 ]] && fast=(-e 's|scrape_interval: 15s|scrape_interval: 5s|' -e 's|evaluation_interval: 15s|evaluation_interval: 5s|')
  # Every reference, including the http_sd URLs (/sd/dns, /sd/nodes, /sd/clients) behind ping and DNS jobs.
  sed -e "s|asus-collector:9877|$host:${LISTEN_PORT:-9877}|g" ${fast[@]+"${fast[@]}"} \
    config/prometheus/prometheus.yml > dev/.prometheus.local.yml
}

# Dashboards with links that work on this machine (Speedtest on localhost; NetAlertX only runs on Linux).
local_dashboards() {
  NETALERTX_URL="" SPEEDTEST_APP_URL="http://localhost:$SPEEDTEST_PORT" \
    python3 scripts/build_dashboards.py --out dev/.dashboards >/dev/null
}

containers_up() {  # containers_up [fake]
  local svcs=("${SERVICES[@]}")
  free_port GRAFANA_PORT 3000; free_port PROMETHEUS_PORT 9090; free_port SPEEDTEST_PORT 18765
  prometheus_config; local_dashboards
  [[ ${1:-} == fake ]] && { dev_key; svcs+=(fake-router); }
  profile_services
  info "starting containers: ${svcs[*]}"
  "${compose[@]}" up -d --build "${svcs[@]}" >/dev/null
  sync_grafana_password
  echo "  Grafana     http://localhost:${GRAFANA_PORT:-3000}  (user ${GRAFANA_ADMIN_USER:-admin}, password from GRAFANA_ADMIN_PASSWORD in .env)"
  echo "  Prometheus  http://localhost:${PROMETHEUS_PORT:-9090}/targets"
  echo "  Speedtest   http://localhost:${SPEEDTEST_PORT:-18765}"
}

# Optional services named in COMPOSE_PROFILES (.env), as `docker compose up` would start them.
profile_services() {
  local p profs=()
  IFS=, read -ra profs <<<"${COMPOSE_PROFILES:-}"
  for p in ${profs[@]+"${profs[@]}"}; do
    case $p in
      iperf3) svcs+=(iperf3 pushgateway) ;;
      digest) svcs+=(digest) ;;
      netalertx)
        # Host networking on macOS means the podman/Docker VM, not your LAN: its ARP scan would find nothing
        # and its port isn't forwarded to this Mac. It needs a Linux host.
        if [[ $(uname -s) == Linux ]]; then svcs+=(netalertx)
        else echo "  (skipping netalertx: it needs host networking on a Linux machine, see README)"; fi ;;
      *) echo "  (unknown profile in COMPOSE_PROFILES: $p)" ;;
    esac
  done
}

# Grafana only reads GF_SECURITY_ADMIN_PASSWORD on its first start; keep it in step with .env.
sync_grafana_password() {
  local id
  id=$($engine ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=grafana" | head -n1)
  [[ -n $id ]] || return 0
  for _ in $(seq 30); do curl -sf "localhost:${GRAFANA_PORT:-3000}/api/health" >/dev/null && break; sleep 1; done
  $engine exec "$id" grafana cli admin reset-admin-password "${GRAFANA_ADMIN_PASSWORD:?}" >/dev/null 2>&1 \
    || echo "  (could not sync the Grafana password; it may still be the one from first start)"
}

run_collector() {  # run_collector [fake]
  venv
  # Localhost only: containers reach it through the engine's host alias.
  export LISTEN_PORT=${LISTEN_PORT:-9877} LISTEN_ADDR=${LISTEN_ADDR:-127.0.0.1} LOG_LEVEL=${LOG_LEVEL:-INFO}
  [[ ${FAST:-} == 1 ]] && export FAST_INTERVAL=5 SLOW_INTERVAL=30
  # Same optional files as the container; device history goes to a git-ignored folder.
  export DEVICES_FILE=${DEVICES_FILE:-$ROOT/config/collector/devices.yml} OUI_FILE=${OUI_FILE:-$ROOT/config/collector/manuf} \
    STATE_DIR=${STATE_DIR:-$ROOT/dev/.state}
  if [[ ${1:-} == fake ]]; then
    dev_key
    export ROUTER_HOST=127.0.0.1 ROUTER_PORT=2222 ROUTER_USER=root ROUTER_KEY_PATH=$ROOT/secrets/dev_key \
      MESH_NODES=none FAST_INTERVAL=${FAST_INTERVAL:-10} SLOW_INTERVAL=${SLOW_INTERVAL:-30}
  else
    # The container path from .env doesn't exist here; use the key in secrets/.
    [[ ${ROUTER_KEY_PATH:-} && -f ${ROUTER_KEY_PATH:-} ]] || export ROUTER_KEY_PATH=$ROOT/secrets/router_key
    [[ -f $ROUTER_KEY_PATH ]] || die "no SSH key at $ROUTER_KEY_PATH (see secrets/README.md)"
    nc -z -G 3 "$ROUTER_HOST" "${ROUTER_PORT:-22}" 2>/dev/null \
      || die "nothing listening on $ROUTER_HOST:${ROUTER_PORT:-22}. Enable SSH (LAN only) in the router UI, or use ./dev.sh fake"
  fi
  info "collector -> ${ROUTER_USER}@${ROUTER_HOST}:${ROUTER_PORT:-22}, polls every ${FAST_INTERVAL:-15}s/${SLOW_INTERVAL:-300}s, metrics on http://localhost:$LISTEN_PORT/metrics (Ctrl-C to stop)"
  cd collector
  exec .venv/bin/watchfiles --filter python '.venv/bin/python -m netmon_collector.main' netmon_collector
}

# A value from .env.example (the committed defaults), ignoring .env.
example_env() { sed -n "s/^$1=//p" .env.example | head -n1; }

check() {
  venv
  local py=collector/.venv/bin tmp
  info "ruff";            (cd collector && .venv/bin/ruff check . && .venv/bin/ruff format --check .)
  info "pytest";          (cd collector && .venv/bin/pytest -q)
  info "dashboards";      for f in dashboards/*.json; do $py/python -m json.tool "$f" >/dev/null || die "bad JSON: $f"; done
  # dashboards/ must match the generator, built with the committed (.env.example) URLs.
  tmp=$(mktemp -d)
  NETALERTX_URL=$(example_env NETALERTX_URL) SPEEDTEST_APP_URL=$(example_env SPEEDTEST_APP_URL) \
    $py/python scripts/build_dashboards.py --out "$tmp" >/dev/null
  if ! diff -r dashboards "$tmp" >/dev/null; then
    rm -rf "$tmp"
    die "dashboards/ is out of date: run NETALERTX_URL=$(example_env NETALERTX_URL) SPEEDTEST_APP_URL=$(example_env SPEEDTEST_APP_URL) python3 scripts/build_dashboards.py"
  fi
  rm -rf "$tmp"
  info "shellcheck"
  if command -v shellcheck >/dev/null 2>&1; then
    shellcheck dev.sh scripts/*.sh dev/fake-router/wl dev/fake-router/nvram dev/fake-router/entrypoint.sh
  else echo "  (shellcheck not installed; skipped)"; fi
  info "promtool"
  if command -v promtool >/dev/null 2>&1; then promtool test rules config/prometheus/tests/*.yml >/dev/null
  else echo "  (promtool not installed; skipped)"; fi
  info "actionlint"
  if command -v actionlint >/dev/null 2>&1; then actionlint
  else echo "  (actionlint not installed; skipped)"; fi
  info "compose config";  pick_engine; prometheus_config; SPEEDTEST_APP_KEY=x GRAFANA_ADMIN_PASSWORD=x ROUTER_HOST=x \
                            "${compose[@]}" config >/dev/null
                          "${compose_base[@]}" --env-file dev/dev.env -f docker-compose.yml -f dev/docker-compose.dev.yml \
                            config >/dev/null
  info "all checks passed"
}

cmd=${1:-up}; shift || true
case $cmd in
  up)        load_env; pick_engine; containers_up; run_collector ;;
  fake)      load_env; pick_engine; containers_up fake; run_collector fake ;;
  collector) load_env; run_collector "${1:-}" ;;
  containers) load_env; pick_engine; containers_up "${1:-}" ;;
  check)     load_env; check ;;
  release)   exec scripts/release.sh "$@" ;;
  recon)     load_env; export ROUTER_PORT=${ROUTER_PORT:-22}; scripts/router-recon.sh "${ROUTER_USER:-admin}@${ROUTER_HOST:?}" "${ROUTER_KEY_PATH:-secrets/router_key}" ;;
  ps)        load_env; pick_engine; "${compose[@]}" ps ;;
  logs)      load_env; pick_engine; "${compose[@]}" logs --tail 100 -f "$@" ;;
  stop)      load_env; pick_engine; "${compose[@]}" stop ;;
  down)      load_env; pick_engine; "${compose[@]}" down "$@" ;;
  *)         sed -n '2,17p' "$0"; exit 1 ;;
esac
