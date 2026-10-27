---
name: okfmem-save
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
description: "Session close-out — clean up tool-created worktrees/branches, write active state to STATE.md, capture durable insights as memory pages, and commit + push okfmem-store via `okfmem sync`. Drafts an impl-complete comment on the resolved GitHub issue via `gh issue comment`. Invoked as /okfmem-save (alias: /primer)."
---

Resolve STORE_PATH from the `store` field of `okfmem init --resolve-project`
with the user's explicit `--store PATH` when supplied. Pass that exact store
with `--store "$STORE_PATH"` to every init, pull, sync, graduate, status, and
maintenance command in this skill. For an explicit memory directory, resolve
its enclosing store before store-level mutation; stop if ambiguous.
Use the installed `okfmem` CLI. If it is off PATH, resolve the real path of this
SKILL.md and use the engine dispatcher two directories above it; do not assume
a home-directory checkout. SKILL_DIR is the real directory containing this skill.

# /okfmem-save — Write active state + capture insights + commit & push okfmem-store

> **Names.** Canonical `/okfmem-save`; `/primer` is a back-compat alias (a
> symlink at `~/tools/skills/primer` → this skill). Both invoke the same
> process. This skill and the memory engine live in the `okfmem` repo
> (`~/okfmem/skills/`); `okfmem init` symlinks it into each harness.

`/okfmem-save` closes out a work session in one move: overwrite the project's bounded active state (`STATE.md`), **capture durable insights as per-project markdown memory pages**, optionally post an impl-complete comment on the resolved GitHub issue, and commit + push `~/okfmem-store` synchronously at the end.

**Everything is plain markdown under `~/okfmem-store` — no MCP, no daemon.** Active state and durable insights are files the native memory system auto-loads next session (`STATE.md`, `MEMORY.md`, and the `<topic>.md` pages). This skill writes them directly, in-session, on whatever model the session is on, then runs the final `git` commit/push via the shared `okfmem sync` helper.

## Store layout (for context)

The store is a flat, label-indexed pool (#97). Write to the store, never to the view:

- **Durable knowledge** lives in `$STORE_PATH/pages/<slug>.md`: one pool for every repo. Each page is OKF v0.1 (markdown + YAML frontmatter with a top-level `type:` field ∈ `user` | `feedback` | `project` | `reference`) and carries `labels: [repo:<project>]` (plus `org:`/`stack:` labels when it applies more widely).
- **Active state** lives in `$STORE_PATH/state/<project>.md`: a bounded, single-session snapshot with a fixed 6-section shape (Summary / Left off / Next steps / Decisions / Blockers / Goal) plus OKF `type: state` frontmatter. It is **overwritten every session**, never appended. `type: state` is a separate kind, not a fifth value in the page `type:` enum.
- **The view** `$MEMORY_DIR` = `$STORE_PATH/views/<project>/` is what harnesses auto-load. It is **compiled and gitignored**: its `MEMORY.md` and `MEMORY-lane-*.md` indexes are generated from page labels (one line per page from its `name` + `description`), its `pages` links to the pool, and its `STATE.md` links to (or, on Windows without symlink rights, copies) the state file. **Never write into `$MEMORY_DIR`**: a page there is never synced, an index edit is overwritten on the next compile, and on Windows a `STATE.md` written there is a local copy, not the synced state.

## When to use

End-of-session. Also works mid-session for a checkpoint.

Draft memory directly in the current session. Optional issue comments can also be drafted inline; delegation is never required.

## Process

### Step 1: Identify the active project

Run `okfmem init --resolve-project` (read-only JSON). Honor an explicit store
with `--store PATH`, otherwise `$OKFMEM_STORE` or the default store.
Use `project`, `root`, `memory_dir`, and `store` as PROJECT_NAME, PROJECT_ROOT, MEMORY_DIR, and STORE_PATH.
Set PAGES_DIR=`$STORE_PATH/pages` and STATE_FILE=`$STORE_PATH/state/$PROJECT_NAME.md`; those are the only
write targets. Proceed only when `state` is `ready` and memory_dir is nonempty. For
`uninitialized`, run `okfmem init --yes --store STORE_PATH` from the repository and resolve again.
For `not-a-repo` or an unreachable engine, stop. Never write into `projects/` (retired) or into `$MEMORY_DIR` (compiled, gitignored).
Run `okfmem pull --quiet --store "$STORE_PATH"` before reading existing memory.
After pulling, establish the active-state baseline by checking state:
```bash
okfmem state check --store "$STORE_PATH" --project "$PROJECT_NAME" --session "$SESSION_ID"
```
If the harness has no session ID (e.g. Codex/AGY), record the initial state file's `modified:` timestamp and content hash to use as the fallback baseline (`--since <modified> --hash <hash>`).
Claude native links remain supported; Codex and AGY use this store path directly.
No cleanup script or harness-specific agent API is required to save memory.

### Step 2: Triage this session's content

First read the cross-session feed. The session model sees only this session; `okfmem distill` sees topics that recurred across many sessions and still have no page. It is a read-only reporter (writes nothing, zero API calls), and an opt-in plugin, so a core install without `plugins/memory_distill.py` exits non-zero. That is not an error: skip the feed and continue the close-out. Use the `PROJECT_NAME` from Step 1's `okfmem init --resolve-project` result (so registry renames resolve), never `basename $(pwd)`:

```bash
okfmem distill --store "$STORE_PATH" --project "$PROJECT_NAME" --limit 10 \
  || "$SKILL_DIR/../../okfmem" distill --store "$STORE_PATH" --project "$PROJECT_NAME" --limit 10 \
  || true
```

A candidate is a *proposal*, not an instruction: the Step 3 capture bar still governs. Note N, the number of candidates surfaced, for Step 8. The feed is capped by `--limit`, so when N equals the limit (10) more candidates may exist: say so in Step 8.

For each durable item worth preserving across sessions, pick the right home:

| Content | Goes to |
| -- | -- |
| A `distill` candidate that clears the Step 3 capture bar and adds a fact no existing page holds | **Memory page** (Step 3). If an existing page covers the topic under a different slug, reuse that exact filename (`Edit`, not a duplicate `Write`) per Step 3's reuse rule |
| A `distill` candidate that is status/derivable, or whose fact an existing page already holds (under any slug) | **Skip** — name it in Step 8 |
| What's in progress, next step, active blocker | **`STATE.md`** (see Step 5) — bounded active state |
| Decision, engineering insight, reusable pattern, "lesson learned" | **Memory page** — `$PAGES_DIR/<slug>.md` labelled `repo:<PROJECT_NAME>` (see Step 3) |
| Bug discovered, follow-up work not yet started | **GitHub issue** — file it |
| What shipped in this session | **`STATE.md`** (`Summary` + `Decisions` if significant) |
| What shipped in prior sessions | **Skip** — `git log` + the issue tracker are authoritative |

### Step 3: Capture durable insights as memory pages

For each durable insight from this session, write (or update) one markdown page in `$PAGES_DIR`. There is no index to edit: the view's `MEMORY.md` is compiled from the pages' labels. Emit these `Write`/`Edit` calls directly on the session model — **do not spawn a Haiku Agent per file.**

The capture bar: decisions, engineering insights, reusable patterns, and lessons learned that will matter across sessions. Skip routine/derivable activity (what shipped, one-off chores) — `git log` and the issue tracker are authoritative for those.

**Never capture what a tool already records.** Before writing a memory page, ask *"could a future session get this from `git log`, `gh pr view`, or the issue tracker in one command?"* If yes, it is not a memory:

- ❌ "PR #447 merged, squash `abc1234`" / "epic #85 closed" / "CI went red because the branch was behind main" / "branch `gh-469-probe` has a worktree at …"
- ❌ issue status, assignee, milestone, review state, a plan for work that is now done
- ✅ the **invariant the PR established** — e.g. "`collectAnchors` rejects a header when the two-column flatten interleaves the date cell" — which survives the PR, the branch, and the issue number

The test is *durability under `git log`*, not importance-at-the-time. Capture the fact **stripped of its status wrapper** — the gotcha, not the changelog. Evidence: one curation pass on a real project deleted 73 of 445 memory entries (16% of the store), almost all status wrappers; age was a dead signal (nothing over 90 days).

Each page is OKF v0.1: markdown with YAML frontmatter.

- **Filename** — a short kebab-case slug, e.g. `gh-api-save-partial-failure.md`. The pool is shared by every repo, so **search all of `$PAGES_DIR` first** (`grep -ril '<topic words>' "$PAGES_DIR"`, not just this repo's pages) and reuse the *exact* existing filename when a page already covers the topic — `Edit` it so it merges instead of duplicating. If that page belongs to another repo and also applies here, add `repo:<PROJECT_NAME>` to its `labels:` rather than copying it.
- **`name`** — the human title. The compiled index shows it as the link text.
- **`description`** — the one-line recall hook. The compiled index shows it after the link.
- **`type`** — `user` | `feedback` | `project` | `reference`.
- **`labels`** — at least `repo:<PROJECT_NAME>`; add `org:<owner>` or `stack:<lang>` only when the fact genuinely holds org-wide or stack-wide (those pages surface in every matching repo's view).

**New topic** → `Write` `$PAGES_DIR/<slug>.md`:

```markdown
---
name: <Title>
description: <one-line hook>
type: <user|feedback|project|reference>
labels: [repo:<PROJECT_NAME>]
---

# <Title>

<the insight, one paragraph per point; for feedback/project, include the why and how>
```

**Existing topic** → `Edit` that page to add the new fact, or supersede stale content in place (no manual `SUPERSEDED` markers needed — just rewrite the page to current truth). Refresh `description` if the hook changed.

**Budget: the compiled index line is ≤150 chars, enforced here at write time (#52).** The line is `- [<name>](pages/<slug>.md) — <description>`; count it before writing. Over 150, tighten `description`: move detail into the page body, where it costs nothing. Advisory, not a hard gate: if it can't be tightened without losing recall value, write it anyway and **report it** in Step 8.

**Recompile the view** after the page writes so this session's pages show up in `$MEMORY_DIR/MEMORY.md` (the compiler routes overflow into lane indexes itself):

```bash
okfmem init --resolve-project --compile --store "$STORE_PATH"
```

Do this step before Step 5 so the `STATE.md` summary can mention what was captured.

### Step 4: File GitHub issues for pending work

Follow-ups, deferred scope, discovered bugs → GitHub issue. Do not list them in `STATE.md` — `STATE.md` is active state only.

### Step 5: Write active state to `STATE.md`

Before writing, run `okfmem state check` to ensure no concurrent session has updated state in the meantime:
```bash
okfmem state check --store "$STORE_PATH" --project "$PROJECT_NAME" --session "$SESSION_ID"
# or fallback: okfmem state check --store "$STORE_PATH" --project "$PROJECT_NAME" --since "<baseline_modified>" --hash "<baseline_hash>"
```
On a mismatch (exit non-zero), another session or machine saved state while this session was active. Do not silently overwrite it! Instead, read the existing state file on disk and **fold its still-live items** into this session's `Left off` and `Next steps` sections, each item tagged with its origin: `(from <machine>, <modified>)`. Keep the fixed 6-section shape and the 8192-byte ceiling. Only prompt the user if items directly contradict. A save whose baseline is older than the current file never silently discards the other snapshot's live items.

Overwrite `$STATE_FILE` (`$STORE_PATH/state/<PROJECT_NAME>.md`) with the current session's active state — never `$MEMORY_DIR/STATE.md`, which is a link or (on Windows) a local copy of it. **`STATE.md` is bounded and OVERWRITTEN each session — never appended.** It always uses this exact 6-section shape:

```markdown
---
type: state
project: <PROJECT_NAME>
modified: <ISO-8601 UTC timestamp — e.g. 2026-07-23T18:04:00Z>
machine: <short hostname — e.g. from socket.gethostname().split('.')[0]>
---

# STATE — <PROJECT_NAME>

## Summary
<one-session summary — what was accomplished this session>

## Left off
<specific file/feature/bug actively being worked on>

## Next steps
<bulleted concrete next actions>

## Decisions
<bulleted: - what — why>

## Blockers
<bulleted blockers, or (none)>

## Goal
<the standing goal>
```

Fill each section from the session. **Stamp `modified:` with the current wall-clock time in ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`), fresh on every save** — `okfmem sync`/`okfmem pull` read this field to auto-resolve cross-machine `STATE.md` conflicts (newer `modified:` wins, last-write-wins), so a stale or missing value silently defers reconciliation to a hand-merge. Stamp `machine:` with the machine's short hostname (`socket.gethostname().split('.')[0]`). Show a draft summary first: `"Session: '<summary>' — save this? (yes / edit)"`. After confirmation, `Write` the file (full overwrite — do not `Edit`/append; the whole file is replaced each session). After a successful write, when `SESSION_ID` is available, run `okfmem state refresh --store "$STORE_PATH" --project "$PROJECT_NAME" --session "$SESSION_ID"` to advance this session's baseline to the saved file before its next save.

**`STATE.md` has a ceiling: 8192 bytes** (~2.2k tokens) — the same auto-load budget `okfmem reindex` checks `MEMORY.md` against (`memory_reindex.STATE_BUDGET_BYTES`; #52). Generous headroom for a bounded, six-section, single-session snapshot, so this is a signal something drifted (a section grew a changelog instead of staying a pointer), not a routine concern. After writing, recompile (refreshes the view's copy on Windows) and spot-check it:

```bash
okfmem init --resolve-project --compile --store "$STORE_PATH"
okfmem reindex --report "${MEMORY_DIR:?Step 1 did not resolve a memory dir — do not fall back to a default}"
# Off PATH: "$SKILL_DIR/../../okfmem" reindex --report "$MEMORY_DIR"
```

Look at the `STATE.md` row's Status column. If it reads `OVER`, report it in Step 8 — advisory, not a rewrite gate.

Keep `## Goal` as the project's standing goal — carry forward the prior value unless the goal shifted this session. Use `(none)` for empty `## Blockers`.

### Step 6: Post impl-complete comment for completed issues

If code committed this session resolves an issue, post an implementation-complete comment on it. The body shape is identical regardless of backend — only the read + write surface differs.

**The tracker is GitHub Issues.** Fetch via `gh issue view <N> --repo <owner>/<repo> --json number,title,body,labels,state` for acceptance criteria; post via `gh issue comment <N> --repo <owner>/<repo> --body-file <tempfile>`; verify with `gh issue view <N> --repo <owner>/<repo> --comments`.

- **`gh repo view` fails** → skip this step and tell the user one line: "no issue tracker detected — skipping impl-complete comment".

Resolve the issue identifier in priority order: (1) the active worktree slug (`.claude/worktrees/<slug>/` — `gh-<N>`), (2) the commit trailer (`Resolves #<N>` / `Refs #<N>`) from `git log -1`, (3) `STATE.md`'s `## Left off` / `## Summary`. If still ambiguous, ask the user.

Steps:

1. Commit hash: `git log --oneline -1`
2. Fetch the issue (as above) — grab acceptance criteria from the body
3. Post the comment with this body:

```markdown
## Implementation Complete — `<commit-hash>`

### Acceptance Criteria

- [x] **Criterion 1** — brief implementation note
- [x] **Criterion 2** — brief implementation note

### Additional Changes

- **Change 1**: description
- **Change 2**: description
```

Rules: check `[x]` with a short note per criterion; use `[ ]` + reason if not met; list additional changes made beyond scope; keep it factual; round-trip verify with `gh issue view --comments`.

**Never write the body and post it in one compound bash command.** `Bash(gh:*)` is allow-listed, but a `cat > /tmp/body.md <<EOF … EOF` heredoc chained with `gh issue comment` does *not* match that prefix rule — the compound falls through to the auto-mode classifier, which gates the embedded external write and blocks the whole invocation. Worse, the tempfile write lived inside the blocked compound, so the retry fails with "no such file." Do it as two discrete steps:

1. **Write the body with the Write tool** (not a bash heredoc) to a tempfile, e.g. `/tmp/<issue>-complete.md`. The Write tool is not subject to the bash-command classifier, so the body always lands.
2. **Post it as a standalone command** so it matches `Bash(gh:*)` and sails through without a gate:
   ```bash
   gh issue comment <N> --repo <owner>/<repo> --body-file /tmp/<issue>-complete.md
   ```
   Keep this on its own — no `cd …;`, no `&&`, no heredoc on the same line.

**Optional delegation (only if the harness supports it):** main session decides which criteria are met and what additional changes shipped. Hand that decided mapping (criterion → "met, note: X" or "unmet, reason: Y", plus the additional-changes list) to an `Agent(model="haiku")` that formats the comment body. The Agent returns the body text; the main session writes it to the tempfile **with the Write tool** and posts via the standalone `gh issue comment` call above, then verifies round-trip.

### Step 7: Commit and push `~/okfmem-store` via `okfmem sync`

Commit + push the memory repo synchronously at the end of the session — no daemon, no queue. This runs **inside the current harness**, so it works the same on Mac and Windows (no LaunchAgent / launchd / cron). The commit+push logic lives in one shared place — the `okfmem sync` engine command (`~/okfmem/memory_sync.py`), which the P3 consolidation Stop-hook job also calls — so the pull-rebase and concurrency-lock behavior is identical on both paths:

```bash
okfmem sync --store "$STORE_PATH" -m "<PROJECT_NAME>: <session summary line>"
# Off PATH: "$SKILL_DIR/../../okfmem" sync --store "$STORE_PATH" -m "…"
```

`okfmem sync` does: `add -A` → (if anything staged) `pull --rebase` → `commit` → `push`, serialized by an `flock` lockfile at the store root so two windows can't race. It is a clean **no-op when nothing changed** (no empty commits). On a `pull --rebase` conflict it aborts the half-done rebase, leaves `~/okfmem-store` clean, refuses to push, and exits non-zero — surface the conflict to the user rather than forcing. It prints one status line (`committed <sha>: <msg>` + `pushed.`, or the reason it did nothing).

### Step 8: Confirm and show

Show the user:
- The session summary written to `STATE.md` (one line)
- Any insights captured as memory pages (slugs + one-line hooks; note new vs. updated-existing)
- The `okfmem distill` feed: `N` candidates surfaced, `k` captured, and the skipped ones named in one line (or `"distill unavailable"` if the plugin is absent); add "feed may be truncated at the limit" when `N` equals the `--limit`. Never truncate silently — that reads as "nothing recurred"
- Any compiled index line for this session's pages over the 150-char budget (slug + length), or `"none"`; likewise if the `okfmem reindex --report` spot-check flagged `STATE.md` as `OVER`
- Any issues filed or commented on
- The memory push result — the commit SHA that was pushed (from `okfmem sync`'s status line), or `"no memory changes to push"` if the working tree was clean.

## Rules

- **Active state goes in `STATE.md`**, not in `MEMORY.md` or the memory pages
- **`STATE.md` is bounded and overwritten every session** — full replace, never append; keep it to the 6-section template
- **Durable knowledge is captured as labelled pages in `$STORE_PATH/pages/`**, not stored in `STATE.md`; active state goes to `$STORE_PATH/state/<project>.md`
- **Never write into `$MEMORY_DIR`** (the compiled, gitignored view) or `projects/` (retired); recompile the view after writing
- **A page's compiled index line is ≤150 chars, checked at write time (#52)** — tighten `description`; if it can't be tightened without losing recall value, write it and report it in Step 8 rather than pass silently
- **`STATE.md` has an 8192-byte ceiling** (`memory_reindex.STATE_BUDGET_BYTES`), spot-checked via `okfmem reindex --report` after Step 5's write; report `OVER`, don't gate on it
- **Reuse an existing page slug to update a topic** (dedup, across the whole pool) — rewrite the page to current truth, or add this repo's label, instead of leaving stale duplicates
- **Pending work goes in the issue tracker** (GitHub Issues), not in `STATE.md` `## Blockers` (reserve blockers for "can't progress" not "haven't started")
- **Capture memory pages BEFORE writing `STATE.md`** so the session summary can reference what was captured
- **Skip reconstructable content** — `git log` + the issue tracker are authoritative for history; don't capture "what shipped"
- **Commit + push `~/okfmem-store` via `okfmem sync`** (Step 7), synchronously, no-op when clean — no daemon, cross-platform; the same helper backs the Stop-hook consolidation job
- Worktree cleanup is optional and independent of memory capture.

## First-time setup on a new machine

Active state and insights are plain markdown under `~/okfmem-store` — no MCP server, no daemon. Setup is the two repo clones plus `okfmem init`:

```bash
git clone git@github.com:116-Labs/okfmem.git      ~/okfmem         # engine (this skill + scripts)
git clone git@github.com:<you>/okfmem-store.git   ~/okfmem-store   # data (markdown pages)
okfmem init   # symlinks skills into each harness + wires memory pointers/registry
# Then run okfmem init from each project repository.
```

The native memory system auto-loads the view's `STATE.md` / `MEMORY.md` on session start, so no load hook is required. If sessions may run on multiple workstations, add a `git -C ~/okfmem-store pull --rebase` SessionStart hook so each session starts from the latest memory.
