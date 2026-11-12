#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh. This REPLACES the current data in each volume in the backup.
#
#   scripts/restore.sh backups/netmonx-backup-<date>.tar        asks before overwriting
#   scripts/restore.sh --yes FILE                               no question (for scripts)
#
# Containers of the project are stopped during the restore and started again afterwards. .env and secrets/
# are only restored if the backup has them and they don't exist yet here (existing files are never touched).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${COMPOSE_PROJECT_NAME:-netmonx}
HELPER=${BACKUP_IMAGE:-alpine:3.22}
yes=false file=
while (($#)); do
  case $1 in
    -y|--yes) yes=true ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "restore.sh: unknown option $1" >&2; exit 2 ;;
    *) file=$1 ;;
  esac
  shift
done
[[ -f $file ]] || { echo "usage: scripts/restore.sh [--yes] backups/netmonx-backup-<date>.tar" >&2; exit 2; }

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then engine=docker
elif command -v podman >/dev/null 2>&1; then engine=podman
else echo "restore.sh: needs docker or podman" >&2; exit 1; fi

# Next to the backup, not /tmp: podman machine on macOS only shares the home directory.
work=$(cd "$(dirname "$file")" && mktemp -d "$PWD/.restore.XXXX")
running=()
cleanup() {
  if ((${#running[@]})); then echo "starting containers again"; "$engine" start "${running[@]}" >/dev/null; fi
  rm -rf "$work"
}
trap cleanup EXIT
tar -C "$work" -xf "$file"
[[ -f $work/MANIFEST ]] || { echo "restore.sh: $file is not a netmonX backup (no MANIFEST)" >&2; exit 1; }
sed 's/^/  /' "$work/MANIFEST"

shopt -s nullglob
archives=()
for a in "$work"/*.tar.gz; do [[ $a == */secrets.tar.gz ]] || archives+=("$a"); done
((${#archives[@]})) || { echo "restore.sh: no volumes in $file" >&2; exit 1; }
vols=()
for a in "${archives[@]}"; do v=$(basename "$a" .tar.gz); vols+=("${PROJECT}_$v"); done
echo "will replace: ${vols[*]}"
if ! $yes; then
  read -r -p "Overwrite these volumes? [y/N] " answer
  [[ $answer == [yY]* ]] || { echo "nothing changed"; exit 1; }
fi

while IFS= read -r id; do running+=("$id"); done < <("$engine" ps -q --filter "label=com.docker.compose.project=$PROJECT")
if ((${#running[@]})); then
  echo "stopping ${#running[@]} containers of project $PROJECT"
  "$engine" stop "${running[@]}" >/dev/null
fi

for a in "${archives[@]}"; do
  v=$(basename "$a" .tar.gz); vol=${PROJECT}_$v
  "$engine" volume inspect "$vol" >/dev/null 2>&1 || "$engine" volume create \
    --label "com.docker.compose.project=$PROJECT" --label "com.docker.compose.volume=$v" "$vol" >/dev/null
  "$engine" run --rm -v "$vol:/data" -v "$work:/backup:ro" "$HELPER" \
    sh -c "find /data -mindepth 1 -delete && tar -C /data -xzf /backup/$v.tar.gz"
  echo "  $vol: restored"
done

if [[ -f $work/devices.yml && ! -f config/collector/devices.yml ]]; then
  cp "$work/devices.yml" config/collector/devices.yml && echo "  config/collector/devices.yml: restored"
fi
if [[ -f $work/secrets.tar.gz ]]; then
  for f in .env secrets/router_key; do
    if [[ -e $f ]]; then echo "  $f: exists, kept"
    else tar -xzf "$work/secrets.tar.gz" "$f" 2>/dev/null && echo "  $f: restored"; fi
  done
fi
echo "done"
