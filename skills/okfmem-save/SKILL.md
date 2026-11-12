---
name: okfmem-save
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
description: "Session close-out — write focus notes on what mattered and hand capture to the background engine (`okfmem capture --now`), which writes pages and STATE.md on a cheaper model and commits the store itself. Falls back to a foreground save when the harness has no runner. Drafts an impl-complete comment on the resolved GitHub issue via `gh issue comment`. Invoked as /okfmem-save (alias: /primer)."
---

Resolve STORE_PATH from the `store` field of `okfmem init --resolve-project`
with the user's explicit `--store PATH` when supplied. Pass that exact store
with `--store "$STORE_PATH"` to every init, pull, sync, graduate, status,
capture, and maintenance command in this skill. For an explicit memory
directory, resolve its enclosing store before store-level mutation; stop if ambiguous.
Use the installed `okfmem` CLI. If it is off PATH, resolve the real path of this
SKILL.md and use the engine dispatcher two directories above it; do not assume
a home-directory checkout. SKILL_DIR is the real directory containing this skill.

# /okfmem-save — Focus notes, background capture, foreground fallback

> **Names.** Canonical `/okfmem-save`; `/primer` is a back-compat alias (a
> symlink at `~/tools/skills/primer` → this skill). Both invoke the same
> process. This skill and the memory engine live in the `okfmem` repo
> (`~/okfmem/skills/`); `okfmem init` symlinks it into each harness.

`/okfmem-save` closes out a work session without blocking the user. The session model does only the part it alone can do: it names what mattered this session and why, as **focus notes**. It then hands the session to the capture engine (`okfmem capture --now`), which triages and captures on a cheaper model in the background, writes the pages and `STATE.md`, and commits and pushes `~/okfmem-store` itself. Where the harness has no capture runner yet, the skill falls back to writing the pages and `STATE.md` in the foreground.

**Decide, don't ask.** No step asks the user whether to save. The save/skip rule lives in `prompts/capture-triage.md`, and when unsure it saves: a wrong save costs little, because decay archives stale pages and each capture is a revertable commit.

**One source of rules.** The capture bar, the page shape and the `STATE.md` shape live in `prompts/capture.md` (`$SKILL_DIR/../../prompts/capture.md`), the same prompt the background engine runs. This skill points at it and never restates it.

## Store layout (for context)

The store is a flat, label-indexed pool (#97). Write to the store, never to the view:

- **Durable knowledge** lives in `$STORE_PATH/pages/<slug>.md`: one pool for every repo, each page labelled `repo:<project>`.
- **Active state** lives in `$STORE_PATH/state/<project>.md`: a bounded, single-session snapshot, overwritten every save, never appended.
- **The view** `$MEMORY_DIR` = `$STORE_PATH/views/<project>/` is what harnesses auto-load. It is **compiled and gitignored**. **Never write into `$MEMORY_DIR`**: a page there is never synced, an index edit is overwritten on the next compile, and on Windows a `STATE.md` written there is a local copy, not the synced state.

## When to use

End-of-session. Also works mid-session for a checkpoint: a later save or the session-end capture covers only what came after it.

## Process

### Step 1: Identify the active project and session

Run `okfmem init --resolve-project` (read-only JSON). Honor an explicit store
with `--store PATH`, otherwise `$OKFMEM_STORE` or the default store.
Use `project`, `root`, `memory_dir`, and `store` as PROJECT_NAME, PROJECT_ROOT, MEMORY_DIR, and STORE_PATH.
Set PAGES_DIR=`$STORE_PATH/pages` and STATE_FILE=`$STORE_PATH/state/$PROJECT_NAME.md`; those are the only
write targets of the fallback. Proceed only when `state` is `ready` and memory_dir is nonempty. For
`uninitialized`, run `okfmem init --yes --store STORE_PATH` from the repository and resolve again.
For `not-a-repo` or an unreachable engine, stop. Never write into `projects/` (retired) or into `$MEMORY_DIR` (compiled, gitignored).
Run `okfmem pull --quiet --store "$STORE_PATH"` before reading existing memory.

Set the harness and SESSION_ID from the harness itself, never by guessing a transcript path:

| Harness | `HARNESS` | `SESSION_ID` |
|---|---|---|
| Claude Code | `claude-code` | `${CLAUDE_SESSION_ID}` (Claude substitutes it in skills) |
| Codex | `codex` | `$CODEX_THREAD_ID` (Codex shells export the thread id) |
| Antigravity | `antigravity` | none |

Never pass a literal `${CLAUDE_SESSION_ID}` under Codex. Then establish the active-state baseline the fallback needs:
```bash
okfmem state check --store "$STORE_PATH" --project "$PROJECT_NAME" --session "$SESSION_ID"
```
With no SESSION_ID (Antigravity), record the state file's `modified:` timestamp and content hash as the fallback baseline (`--since <modified> --hash <hash>`).
Claude native links remain supported; Codex and AGY use this store path directly.

### Step 2: Triage this session's content

First read the cross-session feed. The session model sees only this session; `okfmem distill` sees topics that recurred across many sessions and still have no page. It is a read-only reporter (writes nothing, zero API calls), and an opt-in plugin, so a core install without `plugins/memory_distill.py` exits non-zero. That is not an error: skip the feed and continue. Use the `PROJECT_NAME` from Step 1, never `basename $(pwd)`:

```bash
okfmem distill --store "$STORE_PATH" --project "$PROJECT_NAME" --limit 10 \
  || "$SKILL_DIR/../../okfmem" distill --store "$STORE_PATH" --project "$PROJECT_NAME" --limit 10 \
  || true
```

A candidate is a *proposal*: the capture bar in `prompts/capture.md` still governs. Note N, the number of candidates surfaced, for Step 8; when N equals the limit (10), more may exist.

Sort what this session produced:

| Content | Goes to |
| -- | -- |
| A decision, insight, reusable pattern, lesson, preference, or a `distill` candidate that clears the capture bar | **Focus note** (Step 3) |
| What is in progress, the next step, an active blocker | **Focus note** (Step 3); the engine folds it into `STATE.md` |
| Bug discovered, follow-up work not yet started | **GitHub issue** (Step 4) |
| What shipped, in this session or earlier | **Skip**: `git log` and the issue tracker are authoritative |

### Step 3: Focus notes, then hand off

Write the focus file with the Write tool to `$STORE_PATH/.okfmem/capture/focus-<SESSION_ID>.md` (git-ignored), with the Step 1 SESSION_ID in the name so a session saving on the same store at the same moment cannot overwrite it before this hand-off reads it: **at most 10 bullets**, each naming one thing worth keeping and why it matters, with the page slug to update when you know it (search `$PAGES_DIR` for the topic first). Anything the user explicitly asked to keep goes first. Only the session model knows *why* something mattered; that is all this step adds. Do not draft pages or state here. With nothing to note, hand off without `--focus`: the engine's triage then decides by `prompts/capture-triage.md`.

Hand off. Pass no `--transcript`: the engine resolves the transcript from the harness and session id and validates it.

Claude Code:
```bash
okfmem capture --now --harness claude-code --session "${CLAUDE_SESSION_ID}" --cwd "$PWD" \
  --focus "$STORE_PATH/.okfmem/capture/focus-${CLAUDE_SESSION_ID}.md" --trigger manual --store "$STORE_PATH"
```

Codex:
```bash
okfmem capture --now --harness codex --session "$CODEX_THREAD_ID" --cwd "$PWD" \
  --focus "$STORE_PATH/.okfmem/capture/focus-$CODEX_THREAD_ID.md" --trigger manual --store "$STORE_PATH"
```

Antigravity has no session id and no runner: go straight to the fallback (Step 5) as for exit 3 with unknown identity.

The engine copies the focus text into the job at hand-off, so a later save in the same session cannot change an in-flight request. A focus job is eligible even with one user turn and no tool use: it skips the activity floor and triage.

Act on the exit code:

#### Exit 0: queued

The job is durable, and the engine owns everything else for this save: the page writes, the state file, the view compile, the commit and the push. The skill writes nothing more to the store, so nothing races the worker or commits its partial output under another message. Skip Steps 5 and 7. The closing line is:

`capture queued (focus: N items); okfmem status shows the result`

Say "queued", never "saved".

#### Exit 3: no runner (foreground fallback)

Stdout is JSON: `{"status": "no-runner", "harness": …, "identity": {…} | null, "upto": N | null}`. Keep `upto`. Codex and Antigravity take this path until their runner connectors land. Do Steps 5 and 7, then record coverage (Step 7) so a later runner starts after the saved material.

With `"identity": null` the engine could not resolve this session's transcript. Still capture the current conversation in the foreground (Steps 5 and 7), record nothing, and report `automatic deduplication unavailable (session identity unknown)`. Never guess another session's transcript.

#### Exit 4, or any other failure

Report the failure line from stderr, then take the foreground fallback (Steps 5 and 7) without `--record`. Never print "queued".

### Step 4: File GitHub issues for pending work

Follow-ups, deferred scope, discovered bugs → GitHub issue. Do not list them in focus notes or `STATE.md`: state is active work only. This is a network call and is not bounded by the hand-off; only the hand-off itself is fast.

### Step 5: Foreground fallback — write pages and `STATE.md` (exit 3 or a failed hand-off only)

First apply `prompts/capture-triage.md` to the session: when it says skip and the user asked to keep nothing, write nothing and go to Step 7's skipped record. Otherwise write directly on the session model, with no confirmation, following `prompts/capture.md` for the capture bar, the page frontmatter and body, and the six-section state shape (its "Files okfmem writes from the reply" section shows both files):

- **Pages** go to `$PAGES_DIR/<slug>.md`. The pool is shared by every repo, so search all of `$PAGES_DIR` first (`grep -ril '<topic words>' "$PAGES_DIR"`) and `Edit` the existing page when one covers the topic; if it belongs to another repo and applies here too, add `repo:<PROJECT_NAME>` to its labels instead of copying it. The compiled index line `- [<name>](pages/<slug>.md) — <description>` stays within 150 characters (#52): tighten `description`, or write it anyway and report it in Step 8.
- **State.** Before writing, run `okfmem state check` against the Step 1 baseline:
  ```bash
  okfmem state check --store "$STORE_PATH" --project "$PROJECT_NAME" --session "$SESSION_ID"
  # or: okfmem state check --store "$STORE_PATH" --project "$PROJECT_NAME" --since "<baseline_modified>" --hash "<baseline_hash>"
  ```
  On a mismatch another session or machine saved state meanwhile: fold its still-live items into this session's left-off and next-steps sections, tagged `(from <machine>, <modified>)`, as `prompts/capture.md`'s fold policy says. Then `Write` `$STATE_FILE` whole (never `Edit` or append), stamping `modified:` fresh, since `okfmem sync`/`okfmem pull` resolve cross-machine conflicts by it. With a SESSION_ID, run `okfmem state refresh --store "$STORE_PATH" --project "$PROJECT_NAME" --session "$SESSION_ID"` after the write.
- **Recompile and spot-check** the view (8192-byte ceiling, `memory_reindex.STATE_BUDGET_BYTES`):
  ```bash
  okfmem init --resolve-project --compile --store "$STORE_PATH"
  okfmem reindex --report "${MEMORY_DIR:?Step 1 did not resolve a memory dir — do not fall back to a default}"
  # Off PATH: "$SKILL_DIR/../../okfmem" reindex --report "$MEMORY_DIR"
  ```
  If the `STATE.md` row reads `OVER`, report it in Step 8; it is advisory.

### Step 6: Post impl-complete comment for completed issues

If code committed this session resolves an issue, post an implementation-complete comment on it. Like Step 4, this is a network call outside the hand-off.

**The tracker is GitHub Issues.** Fetch via `gh issue view <N> --repo <owner>/<repo> --json number,title,body,labels,state` for acceptance criteria; post via `gh issue comment <N> --repo <owner>/<repo> --body-file <tempfile>`; verify with `gh issue view <N> --repo <owner>/<repo> --comments`.

- **`gh repo view` fails** → skip this step and tell the user one line: "no issue tracker detected — skipping impl-complete comment".

Resolve the issue identifier in priority order: (1) the active worktree slug (`.claude/worktrees/<slug>/` — `gh-<N>`), (2) the commit trailer (`Resolves #<N>` / `Refs #<N>`) from `git log -1`, (3) `STATE.md`'s left-off or summary section. If still ambiguous, ask the user.

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

### Step 7: Foreground fallback only — commit, push, record coverage

Only after Step 5. Commit and push the store through the shared helper, which holds the same pull-rebase and lock behavior as the engine's own commits:

```bash
okfmem sync --store "$STORE_PATH" -m "<PROJECT_NAME>: <session summary line>"
# Off PATH: "$SKILL_DIR/../../okfmem" sync --store "$STORE_PATH" -m "…"
```

It is a clean no-op when nothing changed. On a `pull --rebase` conflict it aborts the rebase, leaves the store clean, refuses to push and exits non-zero: surface the conflict to the user rather than forcing.

Only after that sync succeeds, and only when exit 3 returned an identity, record the coverage with the `upto` from exit 3's JSON, so a later runner starts after the saved material:

```bash
okfmem capture --record --harness "$HARNESS" --session "$SESSION_ID" --upto <upto> --outcome saved --store "$STORE_PATH"
```

When Step 5 wrote nothing (triage said skip), skip the sync and record with `--outcome skipped` instead. Skip the record after a failed sync, after exit 4, and when the identity was `null`.

### Step 8: Confirm and show

Print exactly one of:

- `queued: N focus items` (exit 0; `okfmem status` later shows the job as `queued`, `saved`, `skipped`, `conflict` or `failed`)
- `saved: N pages, STATE.md written` (fallback), with the commit sha the Step 7 sync printed, and `automatic deduplication unavailable (session identity unknown)` when the identity was `null`
- `skipped: <reason>` (fallback only: by `prompts/capture-triage.md` the session held nothing worth keeping, so nothing was written)

Then add:
- The `okfmem distill` feed: `N` candidates surfaced, `k` turned into focus notes, the skipped ones named in one line (or `"distill unavailable"`); add "feed may be truncated at the limit" when `N` equals the `--limit`.
- On the fallback: any compiled index line over the 150-char budget (slug + length), or `"none"`, and a `STATE.md` flagged `OVER`.
- Any issues filed or commented on.

## Rules

- **Never ask whether to save.** Decide; `prompts/capture-triage.md` holds the rule, and when unsure it saves.
- **The session model writes focus notes, not pages**, on the background path. The engine owns pages, state, compile, commit and push after exit 0.
- **One source of rules**: the capture bar, the page shape and the state shape are in `prompts/capture.md`.
- **Never guess a transcript**: pass harness and session or thread id; the engine resolves the transcript.
- **Never write into `$MEMORY_DIR`** (the compiled, gitignored view) or `projects/` (retired).
- **Pending work goes in the issue tracker** (GitHub Issues), not in focus notes or state.
- **Say "queued" only after exit 0**, and "saved" only after a successful fallback sync.
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
