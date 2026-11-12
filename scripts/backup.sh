#!/usr/bin/env bash
# Back up netmonX data: the Prometheus, Grafana, Speedtest Tracker (sqlite), collector state and NetAlertX volumes.
#
#   scripts/backup.sh [DIR]          write DIR/netmonx-backup-<date>.tar (default DIR: ./backups)
#   scripts/backup.sh --live [DIR]   don't stop the containers first (faster, but Prometheus and the sqlite
#                                    database may be copied mid-write; fine for a quick copy, not for an archive)
#   scripts/backup.sh --with-secrets also store .env and secrets/ (the router key) — keep that file private
#
# Restore with scripts/restore.sh. Containers are stopped while the volumes are copied and started again
# afterwards (a minute or two of missed scrapes). Works with docker or podman.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${COMPOSE_PROJECT_NAME:-netmonx}
VOLUMES=(prometheus_data grafana_data speedtest_data collector_state netalertx_data)
HELPER=${BACKUP_IMAGE:-alpine:3.22}
live=false secrets=false dir=backups
while (($#)); do
  case $1 in
    --live) live=true ;;
    --with-secrets) secrets=true ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "backup.sh: unknown option $1" >&2; exit 2 ;;
    *) dir=$1 ;;
  esac
  shift
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then engine=docker
elif command -v podman >/dev/null 2>&1; then engine=podman
else echo "backup.sh: needs docker or podman" >&2; exit 1; fi

mkdir -p "$dir"
dir=$(cd "$dir" && pwd)
name=netmonx-backup-$(date +%Y%m%d-%H%M%S)
work=$(mktemp -d "$dir/.$name.XXXX")
running=()
cleanup() {
  if ((${#running[@]})); then echo "starting containers again"; "$engine" start "${running[@]}" >/dev/null; fi
  rm -rf "$work"
}
trap cleanup EXIT

if ! $live; then
  while IFS= read -r id; do running+=("$id"); done < <("$engine" ps -q --filter "label=com.docker.compose.project=$PROJECT")
  if ((${#running[@]})); then
    echo "stopping ${#running[@]} containers of project $PROJECT"
    "$engine" stop "${running[@]}" >/dev/null
  fi
fi

found=0
for v in "${VOLUMES[@]}"; do
  vol=${PROJECT}_$v
  if ! "$engine" volume inspect "$vol" >/dev/null 2>&1; then echo "  $vol: not found, skipped"; continue; fi
  "$engine" run --rm -v "$vol:/data:ro" -v "$work:/backup" "$HELPER" tar -C /data -czf "/backup/$v.tar.gz" .
  echo "  $vol: $(du -h "$work/$v.tar.gz" | cut -f1)"
  found=$((found + 1))
done
((found)) || { echo "backup.sh: no volumes of project $PROJECT found" >&2; exit 1; }

if $secrets; then
  tar -czf "$work/secrets.tar.gz" .env secrets 2>/dev/null || echo "  (.env or secrets/ missing; partial secrets copy)"
fi
[[ -f config/collector/devices.yml ]] && cp config/collector/devices.yml "$work/"
printf 'project=%s\ncreated=%s\nversion=%s\n' "$PROJECT" "$(date -u +%FT%TZ)" \
  "$(git describe --tags --always 2>/dev/null || echo unknown)" > "$work/MANIFEST"

tar -C "$work" -cf "$dir/$name.tar" .
$secrets && chmod 600 "$dir/$name.tar"
echo "wrote $dir/$name.tar ($(du -h "$dir/$name.tar" | cut -f1))"
