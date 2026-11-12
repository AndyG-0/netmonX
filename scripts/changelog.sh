#!/usr/bin/env bash
# Read and update CHANGELOG.md (Keep a Changelog format). Used by scripts/release.sh and CI.
#
#   scripts/changelog.sh section VERSION         print that version's notes; a prerelease
#                                                (1.2.0-rc.1) with no section of its own prints Unreleased
#   scripts/changelog.sh has-unreleased          exit 0 if Unreleased has at least one entry
#   scripts/changelog.sh release VERSION [DATE]  print the changelog with Unreleased moved under VERSION
#
# CHANGELOG=path overrides the file; REPO_URL overrides the GitHub URL used in compare links.
set -euo pipefail
file=${CHANGELOG:-$(dirname "$0")/../CHANGELOG.md}
repo=${REPO_URL:-https://github.com/AndyG-0/netmonX}
[[ -f $file ]] || { echo "changelog.sh: no $file" >&2; exit 1; }

# Body of "## [NAME]", without surrounding blank lines. Exit 1 if there's no such heading.
section() {
  awk -v name="$1" '
    /^## \[/ { if (on) exit; if (index($0, "## [" name "]") == 1) { on = 1; found = 1 }; next }
    /^\[[^]]+\]: / { if (on) exit }
    on { line[++n] = $0; if ($0 ~ /[^[:space:]]/) { if (!first) first = n; last = n } }
    END { for (i = first; first && i <= last; i++) print line[i]; exit !found }
  ' "$file"
}

cmd=${1:-}
case $cmd in
  section)
    ver=${2:?usage: changelog.sh section VERSION}
    if ! section "$ver"; then
      [[ $ver == *-* ]] || { echo "changelog.sh: no section for $ver in $file" >&2; exit 1; }
      section Unreleased
    fi ;;
  has-unreleased)
    section Unreleased | grep -qE '^[[:space:]]*[-*] ' ;;
  release)
    ver=${2:?usage: changelog.sh release VERSION [DATE]}
    date=${3:-$(date +%Y-%m-%d)}
    awk -v ver="$ver" -v date="$date" -v repo="$repo" '
      /^## \[Unreleased\]/ { print; print ""; print "## [" ver "] - " date; seen = 1; next }
      /^## \[/ && seen && prev == "" { prev = $0; sub(/^## \[/, "", prev); sub(/\].*/, "", prev) }
      /^\[Unreleased\]: / { links(); next }
      { print }
      function links() {
        print "[Unreleased]: " repo "/compare/v" ver "...HEAD"
        if (prev != "") print "[" ver "]: " repo "/compare/v" prev "...v" ver
        else print "[" ver "]: " repo "/releases/tag/v" ver
        linked = 1
      }
      END { if (!seen) { print "changelog.sh: no ## [Unreleased] heading" > "/dev/stderr"; exit 1 }
            if (!linked) { print ""; links() } }
    ' "$file" ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
