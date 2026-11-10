---
name: okfmem-release
description: Cut an okfmem release with scripts/release.sh — propose the version bump, draft the release notes, open the release PR, and after the merge tag and publish it, each write confirmed after a dry run. Maintainer-only; use when asked to release, cut a version, or tag okfmem.
allowed-tools: Bash(scripts/release.sh status), Bash(scripts/release.sh prepare:*), Bash(scripts/release.sh publish:*), Bash(gh pr view:*), Bash(gh pr checks:*), Bash(gh release view:*), Bash(gh release list:*)
---

# /okfmem-release — cut an okfmem release

`scripts/release.sh` does every mechanical step and every check. This skill makes the two
judgment calls (the version and the notes) and runs the script. Never tag, push a tag, or
create a release by hand. If the script refuses, report its message; don't work around it.

Run from the okfmem checkout on `main`. Notes go in the session scratchpad, never in the repo.

## 1. Status

```bash
scripts/release.sh status
```

If it says "nothing to release", stop and say so. Otherwise propose the version it suggests,
with the reason in one line: a `!` or `BREAKING` commit → major, any `feat` → minor, otherwise
patch. Ask the maintainer to confirm it or name another version. Also propose a short title,
3 to 6 words naming the headline changes (for example `find, MCP server, team stores`).

## 2. Draft the notes

Write `<scratchpad>/release-vX.Y.Z.md` in the house style. Read
`gh release view v<latest> --json body -q .body` to match it.

- One opening line saying what the release is about.
- A plain list of user-visible changes, newest-first by importance, each ending with its
  issue or PR numbers: `` - `okfmem find` ranks durable pages by query (#148). ``
  - Read `gh pr view <n>` for any PR whose subject doesn't say what a user gets.
  - Fold CI, test-only and docs-only commits into one line, or drop them.
  - Name a breaking change first and say what to do about it.
- A closing **Upgrade:** line: `okfmem update` on each machine (`okfmem update --yes` when
  headless), plus any one-time step a change needs, such as re-running `okfmem init`.

No home paths, machine names or personal details: the release is public. Show the draft
and wait for edits or a yes.

## 3. Prepare the release PR

```bash
scripts/release.sh prepare X.Y.Z --notes <scratchpad>/release-vX.Y.Z.md --title "<title>" --dry-run
```

Show the dry run, then ask one yes/no question with the harness's question tool: "Push
`release/vX.Y.Z` and open the release PR?" On yes, run the same command with `--yes` in
place of `--dry-run` and report the PR link. On no, stop.

The maintainer merges the PR (admin bypass while solo; merging is theirs). Watch it with
`gh pr checks <pr> --repo 116-Labs/okfmem` and `gh pr view <pr> --json state`, and go on
once `state` is `MERGED`. If they ask you to wait, stop here and pick up at step 4 later.

## 4. Publish

```bash
scripts/release.sh publish X.Y.Z --notes <scratchpad>/release-vX.Y.Z.md --dry-run
```

Show the dry run: the commit it will tag (it must be the one that set `VERSION`, not
necessarily `main`'s tip) and the release title. Ask one yes/no question: "Tag vX.Y.Z on
<sha> and publish the release?" On yes, run it with `--yes` in place of `--dry-run`.

`allowed-tools` pre-approves `scripts/release.sh` (call it by exactly that relative path, from
the repo root), so these writes run without a permission prompt. The maintainer's yes to that question is
the only gate. If a call is still blocked, hand the maintainer the same command as one `!` line,
and don't retry or work around it.

Then check: `gh release list --repo 116-Labs/okfmem --limit 1 --json tagName,isLatest` shows
vX.Y.Z with `isLatest: true`, and `git rev-parse vX.Y.Z^{commit}` is the commit the dry run named.

## 5. Report

At most 5 lines: the version and title, the PR, the release URL, and the follow-up. Machines
pick it up with `okfmem update`, and any open `needs-check` issue can now name this release.
