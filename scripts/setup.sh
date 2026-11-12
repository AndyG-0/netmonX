#!/usr/bin/env bash
# First-time setup: writes .env, makes the router SSH key, checks the login and fills in the router and mesh
# node IPs in config/smokeping/config.yml.
#
#   scripts/setup.sh          asks each question (Enter keeps the value in [brackets])
#   scripts/setup.sh --yes    takes every default without asking (detected values, generated passwords)
#
# Safe to run again: it starts from the current .env and only changes what you answer. On the router it only
# runs `nvram get` (read-only), the same as the collector.
# shellcheck disable=SC2016  # single-quoted commands are meant to expand on the router
set -euo pipefail
cd "$(dirname "$0")/.."

yes=false
case ${1:-} in
  -y|--yes) yes=true ;;
  -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "setup.sh: unknown option $1" >&2; exit 2 ;;
esac

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

# --- .env helpers (values may contain spaces, * and /, so no sed) ---
get_env() { # get_env KEY: current value in .env, or empty
  local line
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line == "$1="* ]]; then line=${line#*=}; line=${line%\"}; echo "${line#\"}"; return; fi
  done < .env
}
set_env() { # set_env KEY VALUE: replace the KEY= line, or append it
  local line done=false tmp
  tmp=$(mktemp .env.XXXX)
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line == "$1="* ]]; then printf '%s=%s\n' "$1" "$2"; done=true; else printf '%s\n' "$line"; fi
  done < .env > "$tmp"
  $done || printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chmod 600 "$tmp" && mv "$tmp" .env
}
prompt() { # prompt VAR "question" DEFAULT: set VAR to the answer
  local answer=$3
  if ! $yes; then
    read -r -p "$2 [$3]: " answer || true
    answer=${answer:-$3}
  fi
  printf -v "$1" '%s' "$answer"
}
ask() { prompt "$@"; set_env "$1" "${!1}"; } # same, and store it in .env
yesno() { # yesno "question" DEFAULT(y|n): returns 0 for yes
  local answer=$2
  $yes || { read -r -p "$1 [$2]: " answer || true; answer=${answer:-$2}; }
  [[ $answer == [yY]* ]]
}
keep_or() { local v; v=$(get_env "$1"); if [[ -n $v && $v != "$2" ]]; then echo "$v"; else echo "$3"; fi; }

# --- detect what we can ---
gateway() {
  if command -v ip >/dev/null 2>&1; then ip route show default 2>/dev/null | awk '{print $3; exit}'
  else route -n get default 2>/dev/null | awk '/gateway:/{print $2}'; fi
}
host_ip() {
  if command -v ip >/dev/null 2>&1; then
    ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}'
  else
    local ifc; ifc=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
    [[ -n $ifc ]] && ipconfig getifaddr "$ifc" 2>/dev/null
  fi
}
timezone() {
  if command -v timedatectl >/dev/null 2>&1; then timedatectl show -p Timezone --value 2>/dev/null
  elif [[ -f /etc/timezone ]]; then cat /etc/timezone
  else readlink /etc/localtime 2>/dev/null | sed 's|.*zoneinfo/||'; fi
}

if [[ -f .env ]]; then info "updating the existing .env"
else cp .env.example .env && chmod 600 .env && info "created .env from .env.example"; fi

info "Host"
set_env HOST_UID "$(id -u)"
set_env HOST_GID "$(id -g)"
ask TZ "Time zone" "$(keep_or TZ America/Chicago "$(timezone)")"
myip=$(host_ip || true)
host=; prompt host "This machine's LAN IP (for dashboard links)" "${myip:-127.0.0.1}"

info "Router"
gw=$(gateway || true)
ask ROUTER_HOST "Router IP" "$(keep_or ROUTER_HOST 192.168.50.1 "${gw:-192.168.50.1}")"
ask ROUTER_PORT "Router SSH port (Administration → System)" "$(get_env ROUTER_PORT)"
ask ROUTER_USER "Router admin user name" "$(get_env ROUTER_USER)"

info "Grafana and Speedtest Tracker"
pw=$(get_env GRAFANA_ADMIN_PASSWORD)
[[ -z $pw || $pw == change-me ]] && pw=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
ask GRAFANA_ADMIN_PASSWORD "Grafana admin password" "$pw"
[[ -n $(get_env SPEEDTEST_APP_KEY) ]] || set_env SPEEDTEST_APP_KEY "base64:$(openssl rand -base64 32)"
set_env NETALERTX_URL "http://$host:20211"
set_env SPEEDTEST_APP_URL "http://$host:$(get_env SPEEDTEST_PORT)"
ask ISP_PLAN_DOWN "Download speed you pay for, Mbit/s (empty: skip)" "$(get_env ISP_PLAN_DOWN)"
ask ISP_PLAN_UP "Upload speed you pay for, Mbit/s (empty: skip)" "$(get_env ISP_PLAN_UP)"
profiles=()
yesno "Start NetAlertX for device discovery? (needs host networking)" y && profiles+=(netalertx)
ask NTFY_URL "ntfy topic URL for a weekly digest (empty: no digest)" "$(get_env NTFY_URL)"
[[ -n $NTFY_URL ]] && profiles+=(digest)
set_env COMPOSE_PROFILES "$(IFS=,; echo "${profiles[*]-}")"

info "Router SSH key"
mkdir -p secrets
if [[ -f secrets/router_key ]]; then echo "secrets/router_key exists, keeping it"
else
  ssh-keygen -q -t ed25519 -N '' -C netmonx -f secrets/router_key
  chmod 600 secrets/router_key
  echo
  echo "Paste this line into the router's web UI: Administration → System → Authorized Keys, then Apply."
  echo "Also set Enable SSH to \"LAN only\" (never LAN & WAN)."
  echo
  cat secrets/router_key.pub
  echo
  $yes || read -r -p "Press Enter when done... " _ || true
fi

ssh_opts=(-o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p "$ROUTER_PORT"
          -i secrets/router_key)
info "Checking the login to $ROUTER_USER@$ROUTER_HOST:$ROUTER_PORT"
if out=$(ssh "${ssh_opts[@]}" "$ROUTER_USER@$ROUTER_HOST" \
           'nvram get productid; nvram get lan_ipaddr; nvram get cfg_device_list' 2>&1); then
  model=$(sed -n 1p <<<"$out") lan=$(sed -n 2p <<<"$out") devices=$(sed -n 3p <<<"$out")
  echo "ok: $model at ${lan:-$ROUTER_HOST}"
  # cfg_device_list: <model>ip>mac>... per node; the main router is in it too.
  hosts=$( (echo "${lan:-$ROUTER_HOST}"; tr '<' '\n' <<<"$devices" | awk -F'>' '$2 ~ /^[0-9.]+$/ {print $2}') \
           | awk '!seen[$0]++')
  echo "router and mesh nodes: $(tr '\n' ' ' <<<"$hosts")"
  conf=config/smokeping/config.yml
  if grep -q '# main router and AiMesh nodes' "$conf"; then
    tmp=$(mktemp "$conf.XXXX")
    if awk -v hosts="$(paste -sd, - <<<"$hosts")" '
      /# main router and AiMesh nodes/ { print; n = split(hosts, h, ","); for (i = 1; i <= n; i++) print "      - " h[i]; skip = 1; next }
      skip && /^      - / { next }
      { skip = 0; print }' "$conf" > "$tmp"; then chmod 644 "$tmp" && mv "$tmp" "$conf" && echo "updated $conf"
    else rm -f "$tmp"; warn "could not update $conf; add the IPs above by hand"; fi
  fi
else
  warn "SSH login failed:"
  printf "   %s\n" "$out" >&2
  warn "Check that SSH is on (LAN only), the port, the user name and the pasted key, then run this again."
fi

echo
info "Done. Start with: docker compose pull && docker compose up -d"
echo "Grafana: http://$host:$(get_env GRAFANA_PORT) (user $(get_env GRAFANA_ADMIN_USER))"
