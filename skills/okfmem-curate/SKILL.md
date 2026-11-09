---
name: okfmem-curate
description: "Judgment-driven memory curation (rare; routine hygiene is automatic via the okfmem consolidation Stop hook). Inventory pages, flag stale ones, propose deletions/merges/relabels/description rewrites, then recompile the view. Hard approval gate before any deletion. Invoked as /okfmem-curate (alias: /memory-curate)."
origin: user
---

Resolve STORE_PATH from the `store` field of `okfmem init --resolve-project`
with the user's explicit `--store PATH` when supplied. Pass that exact store
with `--store "$STORE_PATH"` to every init, pull, sync, graduate, status, and
maintenance command in this skill. For an explicit memory directory, resolve
its enclosing store before store-level mutation; stop if ambiguous.
Use the installed `okfmem` CLI. If it is off PATH, resolve the real path of this
SKILL.md and use the engine dispatcher two directories above it; do not assume
a home-directory checkout. SKILL_DIR is the real directory containing this skill.

# okfmem Curate

> **Names.** Canonical `/okfmem-curate`; `/memory-curate` is a back-compat
> alias (a symlink at `~/tools/skills/memory-curate` → this skill). This skill
> lives in the `okfmem` repo (`~/okfmem/skills/`); `okfmem init` symlinks it
> into each harness.

> **Fallback tool.** Routine memory hygiene is automatic: the okfmem consolidation
> job (`memory_consolidate.py`, wired to the session-end Stop hook) decay-scores
> every page and archives stale ones (`pages/archive/<year>/`, never deleted),
> recompiling the view every session. Reach for `/okfmem-curate` only for
> **judgment-driven** curation decay does not do: merging duplicates, semantic
> consolidation, relabelling, or a hard purge the archive gate is too
> conservative to make. Consolidation archives (reversible); this skill can
> delete (hard, gated). Prefer letting decay do the routine work.

Curate the label-indexed page pool at `<STORE_PATH>/pages/` (plus `pages/archive/`). The actions are **delete**, **merge**, **relabel**, **tighten `description`**, and **graduate** into `CLAUDE.md`. Pages and their labels are the synced source of truth; the session's `MEMORY.md` and lane indexes are a compiled, gitignored view generated from them.

**Never edit a compiled `MEMORY*.md` index, and never write into `views/` or `projects/`.** An edit there never syncs and is overwritten by the next compile. To change what the index shows, change the page's `description` or `labels` and recompile.

**Hard rule:** no page is deleted or rewritten until the user has explicitly approved the plan.

## When to use

- The user says "clean up memory", "prune memory", "memory hygiene", "context is bloated", or similar.
- Periodic curation (monthly, or after a milestone where many "X landed" pages accumulate).
- **The auto-load byte ceiling is tripped.** When `okfmem status` or `okfmem reindex --report` flags the view as over the ceiling (`memory_reindex.MEMORY_BUDGET_BYTES`, 8192 bytes), the remedy is to tighten page descriptions and relabel pages (so the compiler routes them to lane indexes), then recompile. Deleting pages helps only because it removes lines from the compiled view.
- **Audit-only check-in**: when the user wants the report without committing to changes yet, invoke with the `audit` argument.

## Modes

| Mode | Trigger | What it does |
|------|---------|--------------|
| Default | "clean up memory" / "prune memory" / no arg | Phases 1–5; halts at the approval gate; executes on approval |
| Audit | `audit` arg, or "memory audit" / "what's stale" | Phases 1–3 only; produces the report; no approval gate, no execution |
| Merge a retired repo | `merge <OLD> into <NEW>`, "fold the old repo's memory into…", or init's "decodes to …, which does not exist" warning | The section below instead of the phases; halts at its own approval gate |

## Merge a retired repo

Use this when a repo was renamed, absorbed, or abandoned and its memory should live
under another repo. On the flat pool the mechanical part is small: relabel pages,
fold state, pin the registry. The real work is judgment: what is still true, what
duplicates an existing page, and which repo it belongs to now. A domain split can
send part of an old repo's memory to a third repo. **Ask the user about anything
that isn't clear-cut; never guess a page's relevance.**

1. **Inventory, read-only.** Collect every trace of `<OLD>`:
   - pages labeled `repo:<OLD>` (in `pages/` and `pages/archive/`), and `state/<OLD>.md`;
   - `projects/<OLD>/`, `legacy/`, and `~/.claude/projects/*<OLD>*/memory.bak`;
   - the registry `map` and `overrides` entries that resolve to `<OLD>`;
   - store git history for pages deleted on purpose (`git log --diff-filter=D`). A
     page retired by an earlier curate pass stays retired.

   Match slugs with `_` and `-` treated as the same; older pages were renamed that way.
   Read any page recording an earlier rename or split (search the pool for `<OLD>`)
   before deciding where things belong.
2. **Classify each page**, as one table: *already in `<NEW>`* (skip), *duplicate*
   (merge into the newer page), *relevant* (relabel to `repo:<NEW>`, or add the label
   next to the old one), *belongs elsewhere* (relabel to the repo that owns that domain
   now), *stale* (leave archived, or archive it), or *unsure* (ask). A page that decay
   archived but which is still load-bearing (a standing election, a carryover, an open
   obligation) can be restored: move it back to `pages/`, set `status: active`, drop
   `archived_on`, and set `pinned: true` if it must not decay again.
3. **Approval gate.** Show the table and the planned state and registry edits. Write
   nothing until the user approves.
4. **Execute.** Edit the page frontmatter, and fold any live items from
   `state/<OLD>.md` into `state/<NEW>.md`. Add a registry override for any old path
   that is still on disk. Recompile with `okfmem init --resolve-project --compile`
   from `<NEW>`'s checkout, then run `okfmem sync`.
5. **Local leftovers.** Never delete a `~/.claude/projects/<folder>` without checking it
   first. Check it for transcripts (`*.jsonl`), and check whether another folder is a
   symlink to it; after a rename, the old folder is often the new repo's live history.
   Back up before removing a folder that holds only a `memory` link or a `memory.bak`
   whose pages are all in the store, and also remove the stale `views/<OLD>`.

## How it works

Five phases. Phases 4 and 5 are gated on explicit user approval. The lesson behind this skill: an unguarded "cleanup pass" feels efficient until something load-bearing is gone.

### Phase 1: Resolve the store

Run `okfmem init --resolve-project` (read-only JSON), honoring an explicit `--store PATH` or `$OKFMEM_STORE`. Take `store` as STORE_PATH and the repo's label as `repo:<name>`. For `uninitialized`, run `okfmem init --store STORE_PATH` from the repository; for `not-a-repo` or an unreachable engine, stop or ask for an explicit store. Pages live in `$STORE_PATH/pages/`; that is PAGES_DIR for every later phase. If the user passed an explicit store path, use it and skip the probe.

Check that deletions are recoverable by asking git about the **store root**, not the compiled view:

```bash
GIT_ROOT=$(git -C "${STORE_PATH:?Phase 1 did not resolve a store — do not fall back to a default}" rev-parse --show-toplevel 2>/dev/null)
```

A non-empty GIT_ROOT is the recovery mechanism; retain it for Recovery and surface it to the user. An empty GIT_ROOT means the store is not under git: *say so explicitly* — deletions are not recoverable, and the approval gate needs extra confirmation language.

### Phase 2: Inventory (deterministic)

**Auto-loaded bytes — the headline numbers.** Only the compiled index and the state file load at session start, so they are the only thing that costs context. Run the report first:

```bash
okfmem reindex --report
```

Its `auto_loaded` table gives the compiled `MEMORY.md` and `STATE.md` bytes against their ceilings, and its per-section breakdown shows which section holds the bytes. If the `MEMORY.md` row reads `OVER`, plan description tightening and relabelling (and deletions) that shrink the compiled view; do not try to edit the view.

Count pointer lines over the per-line budget:

```bash
okfmem reindex --budget-check
```

It is **advisory** (always exit 0). Each over-budget line comes from a page's `name` + `description`, so the fix is to tighten that page's `description`.

**Page-level detail — store hygiene, not context.** Run the inventory script on the page pool:

```bash
python3 "$SKILL_DIR/scripts/inventory.py" "$STORE_PATH/pages"
```

It emits per-file age, size, frontmatter `type` and `name`, and heuristic flags. Ignore its link-status column and the `orphan` / `dangling` flags: pages are not linked from hand-written indexes, so they do not apply to the compiled view. Filter the table to pages carrying the target `repo:<name>` label (or the labels the user named), and read `labels:` from each page's frontmatter.

Flags that apply:

- `ck_snapshot` — filename starts `ck_` (retired session-end saves; ephemeral by nature). A plan can propose deleting them.
- `landed_doc` — filename or `name` contains `landed` / `_operational` (project-state, prone to age out)
- `superseded_marker` — body contains `SUPERSEDED`, `superseded by`, `replaced by`, or `(Note:`
- `old_45d` / `old_90d` — mtime older than 45 / 90 days

Page count and on-disk bytes cost zero context at session start; they are background for the plan, not a finding. Deleting a page changes the headline numbers only when it removes a line from the compiled view.

These flags are *signals*, not verdicts. Phase 3 turns them into recommendations.

### Phase 3: LLM judgment (cross-read)

For every flagged page, read its frontmatter + first ~30 lines and classify as one of:

- **keep** — pattern memories, user feedback, current operational state. Default for `feedback_*`, named patterns, and the most recent `*_landed` doc per surface.
- **delete** — superseded markers, snapshots older than the most recent two, pure-noise pages fully covered by the project's `CLAUDE.md`, "X landed" docs older than 7 days where the design rules survive in pattern memories.
- **merge** — two or more pages covering the same ground: fold the unique content into the newer page, then delete the others.
- **relabel** — the page is right but carries the wrong or missing `labels:` (e.g. it belongs to another repo now); propose the new label set.
- **tighten** — the page is fine but its `description` is verbose or over the pointer budget; propose a one-line replacement. The compiled index line is built from `name` + `description`.
- **graduate** — a *still-valuable* page whose rule belongs in the always-loaded `CLAUDE.md`: run `okfmem graduate <slug> --store "$STORE_PATH" [--to <dir>/CLAUDE.md]`. It distills and places the rule, mirrors a real-file `AGENTS.md`, and archives the page stamped `graduated_to`. Reserve `delete` for duplicates with no unique content.
- **unsure** — needs user judgment. Surface verbatim with a short question.

**Cross-check against project CLAUDE.md:**

```bash
grep -F -i "$NAME_FROM_FRONTMATTER" "$CWD/CLAUDE.md" 2>/dev/null
```

If the page's `name` or first-line content appears there, it is a drift risk. A page with unique, still-valuable rules → **graduate**; a pure-noise duplicate → **delete**. Prefer `graduate` whenever the content still carries a rule worth keeping.

**Recency rule for `*_landed` docs:** group by surface; keep the most recent in each group; older ones are delete candidates unless they encode unique content covered nowhere else.

### Phase 4: Approval gate (HARD STOP)

Present the plan as a single markdown response:

```markdown
## Cleanup plan for review

### Bucket A — high-confidence delete (N pages)
| Page | Why |
|---|---|

### Bucket B — duplicates with CLAUDE.md (N pages)
| Page | Where it lives in CLAUDE.md | Action (graduate / delete) |
|---|---|---|

### Bucket C — superseded / aged out (N pages)
| Page | Reason |
|---|---|

### Bucket D — merge (N groups)
| Pages | Surviving page | What is folded in |
|---|---|---|

### Bucket E — relabel / tighten description (N pages)
| Page | Current | Proposed |
|---|---|---|

### Unsure / ask user (N pages)
| Page | Question |
|---|---|

**Net effect:** compiled `MEMORY.md` XKB → ~YKB (re-measured after the recompile), reads **0** if no line of the view changes. N pages deleted/archived is a store-hygiene count, reported separately; it is not a context saving by itself.

→ Approve as-is, or call out pages to keep, before I touch anything.
```

**STOP HERE.** Do not invoke any `rm`, `Edit`, or `Write` tool until the user has explicitly approved. Corrections ("keep X", "delete Y too") update the plan and present it again — still no execution. Proceed to Phase 5 only on a clear "approve" / "proceed" / "yes go ahead".

In **audit mode** the skill stops here and reports completion — no approval question, no execution.

### Phase 5: Execute, recompile, sync

Once approved:

1. Delete the approved pages from `$STORE_PATH/pages/` (one batched `rm`).
2. For **merge**, edit the surviving page to carry the folded content, then delete the others. For **relabel** and **tighten**, edit the page's frontmatter (`labels:`, `description:`) in place.
3. For every page marked **graduate**, run `okfmem graduate <slug> --store "$STORE_PATH" --yes` (add `--to <dir>/CLAUDE.md` for a lane-scoped target). Phase 4 **is** the human approval gate, so `--yes` skips graduate's own `[y/N]`; without it a non-interactive run silently skips and prints only a manual hint.
4. Recompile the view from the repo's checkout:

   ```bash
   okfmem init --resolve-project --compile --store "$STORE_PATH"
   ```

5. Check the result:

   ```bash
   okfmem reindex --verify
   okfmem reindex --report
   ```

   `--verify` exits **1** on any dangling pointer or orphan in the compiled view; a non-zero exit means the pages are inconsistent (for example a deleted page still referenced) — fix the pages and recompile before reporting success. Never fix it by editing the compiled index.
6. Verify each **graduate**: confirm the rule landed and the source moved.

   ```bash
   for slug in $GRADUATED_SLUGS; do
     git -C "$REPO_ROOT" diff -- CLAUDE.md AGENTS.md | grep -q '^+' && echo "OK: $slug rule landed" || echo "MISSING: no CLAUDE.md diff for $slug"
     ARCH="$STORE_PATH/pages/archive/$(date -u +%Y)/$slug.md"  # graduate prints the exact path
     [ ! -f "$STORE_PATH/pages/$slug.md" ] && [ -f "$ARCH" ] && grep -q 'graduated_to:' "$ARCH" \
       && echo "OK: $slug archived with graduated_to stamp" \
       || echo "BROKEN: $slug not cleanly archived (still live, missing, or unstamped)"
   done
   ```

   A `MISSING:`/`BROKEN:` line means the graduate did not apply (commonly `--yes` was omitted); re-run it and re-verify.
7. Push the store:

   ```bash
   okfmem sync --store "$STORE_PATH" -m "curate: <short summary>"
   ```

8. Report the numbers from the step-5 `okfmem reindex --report`, in this order: (1) compiled `MEMORY.md` + `STATE.md` bytes before/after against the ceiling and the tokens saved per session (~bytes delta / 3.5) — the number that matters; (2) pages before/after, labelled non-context store hygiene. "We deleted N pages" is never a context saving on its own.

## Recovery

If the user disagrees with deletions:

```bash
git -C "${GIT_ROOT:?Phase 1 found no Git-backed recovery root}" restore .
```

This restores pages deleted since the last commit, and works only if the store is git-backed (checked in Phase 1). After `okfmem sync` has pushed, recover with `git revert` of the curate commit instead, then recompile. If the store is *not* git-backed, the approval gate requires the extra confirmation language ("this is not recoverable via git").

## Style guarantees

- Never delete a page with frontmatter `type: feedback` without explicit user override — feedback pages encode user preferences and are durable by design.
- Never delete a page whose frontmatter `name` is referenced verbatim by another *kept* page's body (cross-reference cluster).
- The most recent `_landed` / `_operational` doc per surface always survives unless explicitly listed for deletion.
- Descriptions stay one line, ≤150 chars.

## Anti-patterns

- **Executing before approval.** Phase 4 is a hard stop.
- **Editing the compiled index or the view.** Change pages and recompile; never touch `MEMORY*.md` or `views/` by hand.
- **Deleting `type: feedback` pages** without explicit user instruction.
- **Treating the inventory script's flags as verdicts.** Flags are signals; the Phase 3 judgment is the verdict.
- **Inventing deletions outside the approved plan.** If Phase 5 reveals extra junk, surface it as a follow-up — don't fold it in silently.
