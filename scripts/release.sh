#!/usr/bin/env bash
# Cut a netmonX release: bump the version, update CHANGELOG.md, commit, tag vX.Y.Z and push.
# Pushing the tag makes GitHub Actions publish the image and create the GitHub Release.
#
#   scripts/release.sh patch|minor|major      0.4.2 -> 0.4.3 | 0.5.0 | 1.0.0
#   scripts/release.sh minor --pre rc         0.4.2 -> 0.5.0-rc.1
#   scripts/release.sh pre                    0.5.0-rc.1 -> 0.5.0-rc.2 (add --pre beta to switch label)
#   scripts/release.sh --finalize             0.5.0-rc.2 -> 0.5.0
#   scripts/release.sh 1.2.3                  an explicit version (must be higher than the current one)
#
# Options:
#   --dry-run       show the new version and file changes; change nothing
#   --yes           don't ask before committing and pushing
#   --allow-empty   release even if CHANGELOG.md has nothing under Unreleased
#   --skip-checks   don't run ./dev.sh check first
#
# The version lives in collector/pyproject.toml. Prereleases leave CHANGELOG.md alone (their notes
# come from Unreleased); the final release moves Unreleased under the version.
set -euo pipefail
cd "$(dirname "$0")/.."

PYPROJECT=collector/pyproject.toml
SEMVER='^([0-9]+)\.([0-9]+)\.([0-9]+)(-([a-z]+)\.([0-9]+))?$'

die() { echo "release.sh: $*" >&2; exit 1; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
usage() { sed -n '2,19p' "$0"; exit "${1:-1}"; }

bump='' pre='' dry=0 yes=0 allow_empty=0 checks=1
while (($#)); do
  case $1 in
    patch|minor|major|pre) bump=$1 ;;
    --finalize|final) bump=final ;;
    --pre) pre=${2:?--pre needs a label, e.g. rc}; shift ;;
    --dry-run|-n) dry=1 ;;
    --yes|-y) yes=1 ;;
    --allow-empty) allow_empty=1 ;;
    --skip-checks) checks=0 ;;
    -h|--help) usage 0 ;;
    [0-9]*) bump=explicit explicit=$1 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
[[ -n $bump ]] || usage
[[ -z $pre || $pre =~ ^[a-z]+$ ]] || die "--pre label must be lowercase letters (rc, beta, alpha)"

current=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$PYPROJECT" | head -n1)
[[ $current =~ $SEMVER ]] || die "version in $PYPROJECT isn't semver: '$current'"
major=${BASH_REMATCH[1]} minor=${BASH_REMATCH[2]} patch=${BASH_REMATCH[3]}
cur_label=${BASH_REMATCH[5]} cur_num=${BASH_REMATCH[6]}

# Exit 0 if semver $1 > $2 (release > prerelease of the same X.Y.Z; labels compare as text).
version_gt() {
  local a b i
  [[ $1 =~ $SEMVER ]]; a=("${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[5]}" "${BASH_REMATCH[6]}")
  [[ $2 =~ $SEMVER ]]; b=("${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[5]}" "${BASH_REMATCH[6]}")
  for i in 0 1 2; do
    ((a[i] > b[i])) && return 0
    ((a[i] < b[i])) && return 1
  done
  [[ -z ${a[3]} && -n ${b[3]} ]] && return 0
  [[ -z ${a[3]} || -z ${b[3]} ]] && return 1
  [[ ${a[3]} > ${b[3]} ]] && return 0
  [[ ${a[3]} < ${b[3]} ]] && return 1
  ((a[4] > b[4]))
}

case $bump in
  major) new="$((major + 1)).0.0" ;;
  minor) new="$major.$((minor + 1)).0" ;;
  patch) new="$major.$minor.$((patch + 1))" ;;
  pre)
    [[ -n $cur_label ]] || die "$current isn't a prerelease; use e.g. 'minor --pre rc'"
    if [[ -z $pre || $pre == "$cur_label" ]]; then new="$major.$minor.$patch-$cur_label.$((cur_num + 1))"
    else new="$major.$minor.$patch-$pre.1"; pre=''; fi ;;
  final)
    [[ -n $cur_label ]] || die "$current isn't a prerelease; nothing to finalize"
    new="$major.$minor.$patch" ;;
  explicit)
    new=$explicit
    [[ $new =~ $SEMVER ]] || die "'$new' isn't a version like 1.2.3 or 1.2.3-rc.1"
    [[ -z $pre ]] || die "--pre doesn't go with an explicit version; write it out (e.g. 1.2.3-rc.1)" ;;
esac
[[ -n $pre && $bump =~ ^(major|minor|patch)$ ]] && new="$new-$pre.1"
[[ -n $pre && ! $bump =~ ^(major|minor|patch|pre)$ ]] && die "--pre only goes with major, minor, patch or pre"
tag="v$new"
prerelease=0; [[ $new == *-* ]] && prerelease=1

# --- preflight -------------------------------------------------------------
info "release $current -> $new"
branch=$(git symbolic-ref --short HEAD 2>/dev/null) || die "not on a branch"
[[ $branch == main ]] || die "releases are cut from main (you're on $branch)"
[[ -z $(git status --porcelain) ]] || die "working tree isn't clean; commit or stash first"
git remote get-url origin >/dev/null 2>&1 || die "no 'origin' remote"
git fetch -q --tags origin main || die "git fetch origin failed"
read -r behind ahead < <(git rev-list --left-right --count origin/main...HEAD)
((behind == 0)) || die "main is $behind commit(s) behind origin/main; pull first"
((ahead == 0)) || die "main has $ahead unpushed commit(s); push them (and let CI pass) first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
[[ -z $(git ls-remote --tags origin "refs/tags/$tag") ]] || die "tag $tag already exists on origin"
if [[ $new == "$current" ]]; then
  # Only for the very first release, when pyproject already holds it.
  [[ -z $(git tag -l 'v*') ]] || die "$new is the current version; pick a higher one"
elif ! version_gt "$new" "$current"; then
  die "$new isn't higher than the current version $current"
fi
if ((!allow_empty)) && ! scripts/changelog.sh has-unreleased; then
  die "CHANGELOG.md has nothing under ## [Unreleased]; add notes or pass --allow-empty"
fi
command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 \
  || echo "  (gh isn't installed or logged in; the push still works, but run links won't be shown)"

# --- prepare changes in a temp folder ------------------------------------------
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
files=("$PYPROJECT")
sed "s/^version = \".*\"$/version = \"$new\"/" "$PYPROJECT" > "$tmp/pyproject.toml"
if ((!prerelease)); then
  # Users copy .env.example, so it pins the newest stable release.
  files+=(.env.example CHANGELOG.md)
  sed "s/^NETMONX_VERSION=.*/NETMONX_VERSION=$new/" .env.example > "$tmp/.env.example"
  scripts/changelog.sh release "$new" > "$tmp/CHANGELOG.md"
fi
target() { case $1 in "$PYPROJECT") echo "$tmp/pyproject.toml" ;; *) echo "$tmp/$1" ;; esac; }

info "changes"
for f in "${files[@]}"; do diff -u "$f" "$(target "$f")" --label "a/$f" --label "b/$f" || true; done
echo
echo "Release notes for $tag:"
if ((prerelease)); then scripts/changelog.sh section "$new"; else CHANGELOG=$tmp/CHANGELOG.md scripts/changelog.sh section "$new"; fi | sed 's/^/  /'
echo

if ((dry)); then info "dry run: nothing changed"; exit 0; fi

if ((checks)); then info "running ./dev.sh check"; ./dev.sh check; fi
# dev.sh check may create .env or touch ignored files, but must not change tracked ones.
[[ -z $(git status --porcelain) ]] || die "./dev.sh check changed tracked files; commit those first"

if ((!yes)); then
  read -r -p "Commit, tag $tag and push to origin? [y/N] " answer
  [[ $answer =~ ^[Yy] ]] || { echo "aborted; nothing changed"; exit 1; }
fi

# --- commit, tag, push ---------------------------------------------------------
for f in "${files[@]}"; do cp "$(target "$f")" "$f"; done
git add -- "${files[@]}"
git commit -q -m "Release $tag"
git tag -a "$tag" -m "netmonX $tag"
if ! git push -q --atomic origin main "$tag"; then
  echo "release.sh: push failed. The commit and tag are local only. Retry with:" >&2
  echo "  git push --atomic origin main $tag" >&2
  echo "or undo with: git tag -d $tag && git reset --hard HEAD~1" >&2
  exit 1
fi

url=$(git remote get-url origin | sed -E 's#^git@github.com:#https://github.com/#; s#^ssh://git@github.com/#https://github.com/#; s#\.git$##')
info "pushed $tag"
echo "  Workflow:  $url/actions/workflows/publish.yml"
echo "  Release:   $url/releases/tag/$tag (appears when the workflow finishes)"
echo "  Image:     ghcr.io/andyg-0/netmonx-collector:$new"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo "  Follow it: gh run watch \$(gh run list --workflow publish.yml --branch $tag --limit 1 --json databaseId -q '.[0].databaseId')"
fi
