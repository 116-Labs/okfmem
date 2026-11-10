#!/usr/bin/env bash
# release.sh — cut an okfmem release the same way every time (maintainer tool).
#
#   scripts/release.sh status
#   scripts/release.sh prepare <X.Y.Z|major|minor|patch> --notes FILE [--title TEXT] [--yes] [--dry-run]
#   scripts/release.sh publish <X.Y.Z> --notes FILE [--yes] [--dry-run]
#
# status   read-only: current VERSION, latest v* tag, commits since it grouped by
#          type, and the suggested bump (BREAKING/! -> major, feat -> minor, else patch).
# prepare  checks main is clean, in sync with origin and green, then makes branch
#          release/vX.Y.Z with one commit that writes VERSION, runs the leak gate,
#          and (after [y/N]) pushes it and opens the release PR with the notes as body.
# publish  after that PR merged: checks origin/main's VERSION and CI, then (after
#          [y/N]) tags the commit that set VERSION and creates the GitHub release.
#
# --dry-run prints every write instead of running it. With no terminal, a [y/N]
# answers no and prints the commands to run; --yes answers yes.
# Env: OKFMEM_RELEASE_REPO=owner/name overrides the repo parsed from origin.
# Needs git, gh (authenticated) and python3. Works with macOS's bash 3.2.
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
REMOTE=origin
CI_CHECK=verify          # the required fan-in check on main (ci.yml)
DRY=0
YES=0
NOTES=""
TITLE=""

die() { echo "release: $*" >&2; exit 1; }
say() { echo "release: $*"; }

# Shell-quote one word for display. Not printf's %q: bash 3.2 splits UTF-8 (the em dash in titles).
q() {
  case "$1" in
    *[!A-Za-z0-9_./:=@%+,-]*|"") printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}

# A write: printed under --dry-run, run otherwise.
run() {
  if [ "$DRY" = 1 ]; then
    printf '+'; local a; for a in "$@"; do printf ' %s' "$(q "$a")"; done; printf '\n'
  else
    "$@"
  fi
}

# [y/N]. --yes and --dry-run answer yes; no terminal answers no.
confirm() {
  [ "$YES" = 1 ] || [ "$DRY" = 1 ] && return 0
  if [ ! -t 0 ]; then
    return 1
  fi
  local ans
  read -r -p "$1 [y/N] " ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

repo_slug() {
  if [ -n "${OKFMEM_RELEASE_REPO:-}" ]; then
    echo "$OKFMEM_RELEASE_REPO"; return
  fi
  local url
  url=$(git remote get-url "$REMOTE")
  url=${url%.git}
  case "$url" in
    *github.com[:/]*) echo "${url##*github.com[:/]}" ;;
    *) die "cannot read owner/name from $REMOTE ($url); set OKFMEM_RELEASE_REPO" ;;
  esac
}

valid_version() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

# 0 when $1 > $2 (both X.Y.Z).
version_gt() {
  local a1 a2 a3 b1 b2 b3
  IFS=. read -r a1 a2 a3 <<<"$1"
  IFS=. read -r b1 b2 b3 <<<"$2"
  [ "$a1" -ne "$b1" ] && { [ "$a1" -gt "$b1" ]; return; }
  [ "$a2" -ne "$b2" ] && { [ "$a2" -gt "$b2" ]; return; }
  [ "$a3" -gt "$b3" ]
}

bump() {  # bump CURRENT major|minor|patch
  local m n p
  IFS=. read -r m n p <<<"$1"
  case "$2" in
    major) echo "$((m + 1)).0.0" ;;
    minor) echo "$m.$((n + 1)).0" ;;
    patch) echo "$m.$n.$((p + 1))" ;;
  esac
}

current_version() { tr -d '[:space:]' < VERSION; }

latest_tag() { git tag --list 'v[0-9]*' --sort=-v:refname | head -n 1; }

# The commits since the latest tag (or all, with none), in git log format $1.
log_since() {
  local tag
  tag=$(latest_tag)
  if [ -n "$tag" ]; then
    git log --format="$1" "$tag..HEAD"
  else
    git log --format="$1" HEAD
  fi
}

suggest_bump() {  # reads "<sha> <subject>" lines on stdin
  local kind=patch line subj
  while IFS= read -r line; do
    subj=${line#* }
    if [[ "$subj" =~ ^[a-z]+(\([^\)]*\))?!: ]] || [[ "$subj" == *BREAKING* ]]; then
      echo major; return
    fi
    [[ "$subj" =~ ^feat(\([^\)]*\))?: ]] && kind=minor
  done
  echo "$kind"
}

# Conclusion of the required check on a commit, or "missing".
ci_state() {
  local out
  out=$(gh api "repos/$(repo_slug)/commits/$1/check-runs" \
        --jq "[.check_runs[] | select(.name == \"$CI_CHECK\")][0].conclusion // \"missing\"" 2>/dev/null) \
    || out=missing
  echo "${out:-missing}"
}

require_ci_green() {
  local state
  state=$(ci_state "$1")
  [ "$state" = success ] || die "CI check '$CI_CHECK' on ${1:0:12} is '$state', not success"
}

tag_exists() {
  git rev-parse -q --verify "refs/tags/v$1" >/dev/null && return 0
  [ -n "$(git ls-remote --tags "$REMOTE" "refs/tags/v$1")" ]
}

require_notes() {
  [ -n "$NOTES" ] || die "--notes FILE is required"
  [ -f "$NOTES" ] || die "notes file not found: $NOTES"
  grep -q '[^[:space:]]' "$NOTES" || die "notes file is empty: $NOTES"
}

# ---------------------------------------------------------------- status

cmd_status() {
  local cur tag lines
  git fetch -q "$REMOTE" --tags || say "warning: fetch failed; showing local state"
  cur=$(current_version)
  tag=$(latest_tag)
  lines=$(log_since '%h %s')
  echo "VERSION      $cur"
  echo "latest tag   ${tag:-(none)}"
  if [ -z "$lines" ]; then
    echo "commits      0 since ${tag:-the start} — nothing to release"
    return 0
  fi
  echo "commits      $(printf '%s\n' "$lines" | wc -l | tr -d ' ') since ${tag:-the start}"
  local group pat
  for group in feat fix perf docs other; do
    case "$group" in
      other) pat='^[0-9a-f]+ (feat|fix|perf|docs)(\(|!|:)' ;;
      *) pat="^[0-9a-f]+ $group(\(|!|:)" ;;
    esac
    local sel
    if [ "$group" = other ]; then
      sel=$(printf '%s\n' "$lines" | grep -Ev "$pat" || true)
    else
      sel=$(printf '%s\n' "$lines" | grep -E "$pat" || true)
    fi
    [ -n "$sel" ] || continue
    echo
    echo "$group:"
    printf '%s\n' "$sel" | sed 's/^/  /'
  done
  local kind
  kind=$(printf '%s\n' "$lines" | suggest_bump)
  # A squash merge keeps the branch's commit body, so the footer alone can mark a break.
  if grep -Eq '^BREAKING[ -]CHANGE:' <<<"$(log_since %B)"; then kind=major; fi
  echo
  echo "suggested    $kind -> $(bump "$cur" "$kind")"
}

# ---------------------------------------------------------------- prepare

cmd_prepare() {
  local want="${1:-}"
  [ -n "$want" ] || die "prepare needs a version or major|minor|patch"
  require_notes
  local cur new branch
  cur=$(current_version)
  case "$want" in
    major|minor|patch) new=$(bump "$cur" "$want") ;;
    *) new=$want ;;
  esac
  valid_version "$new" || die "not a version: $new"
  version_gt "$new" "$cur" || die "v$new is not newer than VERSION $cur"

  [ "$(git symbolic-ref --short -q HEAD || true)" = main ] || die "not on main"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "working tree has uncommitted changes"
  git fetch -q "$REMOTE" --tags
  [ "$(git rev-parse HEAD)" = "$(git rev-parse "$REMOTE/main")" ] \
    || die "main is not even with $REMOTE/main (pull or push first)"
  tag_exists "$new" && die "tag v$new already exists"
  branch="release/v$new"
  git rev-parse -q --verify "refs/heads/$branch" >/dev/null && die "branch $branch already exists"
  require_ci_green "$(git rev-parse HEAD)"

  local subject="chore(release): v$new"
  [ -n "$TITLE" ] && subject="$subject — $TITLE"
  say "VERSION $cur -> $new on $branch"
  run git switch -c "$branch"
  if [ "$DRY" = 1 ]; then
    echo "+ echo $new > VERSION"
  else
    echo "$new" > VERSION
  fi
  run git add VERSION
  run git commit -q -m "$subject"
  run python3 scripts/check-leaks.py

  local slug
  slug=$(repo_slug)
  if confirm "Push $branch and open the release PR on $slug?"; then
    run git push -q -u "$REMOTE" "$branch"
    run gh pr create --repo "$slug" --base main --head "$branch" --title "$subject" --body-file "$NOTES"
    say "merge the PR, then: scripts/release.sh publish $new --notes $NOTES"
  else
    say "not pushed. To finish by hand:"
    printf '  git push -u %s %s\n' "$REMOTE" "$branch"
    printf '  gh pr create --repo %s --base main --head %s --title %s --body-file %s\n' \
      "$slug" "$branch" "$(q "$subject")" "$(q "$NOTES")"
  fi
}

# ---------------------------------------------------------------- publish

# The newest commit on $REMOTE/main whose VERSION reads $1 while its parent's didn't.
version_commit() {
  local sha parent
  for sha in $(git log --format=%H "$REMOTE/main" -- VERSION); do
    [ "$(git show "$sha:VERSION" 2>/dev/null | tr -d '[:space:]')" = "$1" ] || continue
    parent=$(git show "$sha^:VERSION" 2>/dev/null | tr -d '[:space:]' || true)
    [ "$parent" = "$1" ] && continue
    echo "$sha"; return 0
  done
  return 1
}

cmd_publish() {
  local ver="${1:-}"
  [ -n "$ver" ] || die "publish needs X.Y.Z"
  ver=${ver#v}
  valid_version "$ver" || die "not a version: $ver"
  require_notes
  git fetch -q "$REMOTE" --tags
  local on_main
  on_main=$(git show "$REMOTE/main:VERSION" | tr -d '[:space:]')
  [ "$on_main" = "$ver" ] || die "$REMOTE/main VERSION is $on_main, not $ver (is the release PR merged?)"
  tag_exists "$ver" && die "tag v$ver already exists"
  local sha
  sha=$(version_commit "$ver") || die "no commit on $REMOTE/main sets VERSION to $ver"
  require_ci_green "$sha"

  local subject title slug
  subject=$(git log -1 --format=%s "$sha")
  title="v$ver"
  case "$subject" in
    "chore(release): v$ver — "*) title="v$ver — ${subject#"chore(release): v$ver — "}" ;;
  esac
  # A squash merge appends " (#N)"; the release title doesn't need it.
  [[ "$title" =~ ^(.*)\ \(#[0-9]+\)$ ]] && title=${BASH_REMATCH[1]}
  slug=$(repo_slug)
  say "tag v$ver -> ${sha:0:12} ($subject)"
  say "release title: $title"
  if confirm "Tag v$ver, push it and publish the release on $slug?"; then
    # A re-run refuses once the tag exists, so a failed write hands over the rest by hand.
    run git tag -a "v$ver" -m "$title" "$sha" || publish_owed 1
    run git push -q "$REMOTE" "v$ver" || publish_owed 2
    run gh release create "v$ver" --repo "$slug" --verify-tag --title "$title" \
      --notes-file "$NOTES" || publish_owed 3
    if [ "$DRY" = 0 ]; then
      local latest
      latest=$(gh release view --repo "$slug" --json tagName --jq .tagName)
      [ "$latest" = "v$ver" ] || die "published, but the latest release is $latest, not v$ver"
      say "published v$ver: $(gh release view "v$ver" --repo "$slug" --json url --jq .url)"
    fi
  else
    say "not published. To finish by hand:"
    publish_steps 1
  fi
}

# Print publish's write steps from step $1 (1 tag, 2 push, 3 release) as commands.
# Reads cmd_publish's locals ver, title, sha and slug.
publish_steps() {
  if [ "$1" -le 1 ]; then printf '  git tag -a v%s -m %s %s\n' "$ver" "$(q "$title")" "$sha"; fi
  if [ "$1" -le 2 ]; then printf '  git push %s v%s\n' "$REMOTE" "$ver"; fi
  printf '  gh release create v%s --repo %s --verify-tag --title %s --notes-file %s\n' \
    "$ver" "$slug" "$(q "$title")" "$(q "$NOTES")"
}

publish_owed() {
  echo "release: publish stopped at step $1 of 3. To finish by hand:" >&2
  publish_steps "$1" >&2
  exit 1
}

# ---------------------------------------------------------------- main

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

[ $# -ge 1 ] || { usage; exit 2; }
CMD=$1; shift
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --notes) NOTES=${2:?--notes needs a file}; shift ;;
    --notes=*) NOTES=${1#--notes=} ;;
    --title) TITLE=${2:?--title needs text}; shift ;;
    --title=*) TITLE=${1#--title=} ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done

case "$CMD" in
  status)  cmd_status ;;
  prepare) cmd_prepare "${ARGS[@]+"${ARGS[@]}"}" ;;
  publish) cmd_publish "${ARGS[@]+"${ARGS[@]}"}" ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
