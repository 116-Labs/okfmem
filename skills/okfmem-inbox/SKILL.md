---
name: okfmem-inbox
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
description: "Model triage of pending okfmem inbox candidates — dumps the inbox, judges pages against existing memory and repo code using BRIEF.md, asks only unresolved low-confidence items, and applies decisions through `okfmem inbox apply` (or hands off to `okfmem inbox review --web`). Invoked as /okfmem-inbox."
---

Resolve STORE_PATH from the `store` field of `okfmem init --resolve-project`
with the user's explicit `--store PATH` when supplied. Pass that exact store
with `--store "$STORE_PATH"` to every init, pull, sync, graduate, status, and
maintenance command in this skill. For an explicit memory directory, resolve
its enclosing store before store-level mutation; stop if ambiguous.
Use the installed `okfmem` CLI. If it is off PATH, resolve the real path of this
SKILL.md and use the engine dispatcher two directories above it; do not assume
a home-directory checkout. SKILL_DIR is the real directory containing this skill.

# /okfmem-inbox — Model triage of pending inbox candidates

Triage every pending candidate in the okfmem inbox using the session model, the user's existing memory pages, and the project's code on disk. The model judges the whole dump up front, settles what it can by reading pages and code, asks the user **only** the remaining low-confidence items, and applies the result in one validated pass through `okfmem inbox apply`.

> **Hard rule — write only through `okfmem inbox apply`.**
> This skill writes only `<SCRATCH_DIR>/pending.jsonl` (via `okfmem inbox dump`) and `<SCRATCH_DIR>/decisions.json`. **Never write, edit, or delete files under `pages/`, `projects/`, `views/`, or `state/` directly, and never push, delete, or update git refs (`refs/okfmem/inbox/*`) or run `okfmem sync` directly.** `okfmem inbox apply` validates the whole `decisions.json`, gates on `[y/N]`, writes the pages, lease-deletes the handled refs, recompiles the views, and syncs the store.

## Process

### Step 1: Resolve the store and dump the inbox (`okfmem inbox dump --out <scratch>`)

1. Run `okfmem init --resolve-project` (honoring an explicit `--store PATH` or `$OKFMEM_STORE`) to get `store` (`STORE_PATH`), `project` (`PROJECT_NAME`), and `root` (`PROJECT_ROOT`). If the user passed `--into <ATTACHED_STORE>`, forward `--into "$INTO_STORE"` to `inbox dump`, `inbox apply`, and `inbox review --web`.
2. Choose a scratch directory `<SCRATCH_DIR>` **outside** `$STORE_PATH` (`okfmem inbox dump` refuses an `--out` directory inside the store), such as a session scratch directory or `/tmp/okfmem-inbox-<run>`.
3. Dump the pending inbox candidates (read-only):

   ```bash
   okfmem inbox dump --store "$STORE_PATH" --out "$SCRATCH_DIR" --json
   ```

4. Read the JSON output (`{"out": "…/pending.jsonl", "pages": N, "pushes": M}`). When `pages` is `0`, report `"okfmem inbox: nothing pending."` and stop.

### Step 2: Judge each page using `BRIEF.md`

1. Read `$SKILL_DIR/BRIEF.md` (`skills/okfmem-inbox/BRIEF.md`) and `<SCRATCH_DIR>/pending.jsonl`.
2. Judge every dumped page (`p1`..`pN`) by the rules in `BRIEF.md`:
   - **Keep only durable, non-obvious, still-true facts**; drop run narration, status wrappers, leaked secrets, and anything already covered in existing memory pages or `CLAUDE.md` / `AGENTS.md`.
   - **Prefer `merge` into an existing page over `new`**, and fold duplicate candidates in the dump into one decision with `"covers": ["p13", "p18"]`.
   - **Read each merge target in full before choosing it** (from the target store's live pages for that `project`) so `add` never repeats text already on the page and every `replace` entry matches `old` verbatim once.
   - **Spot-check code claims against the repo** when a checkout for the candidate's project is on disk (`PROJECT_ROOT` or the path mapped in `registry.json`).
3. **Past about 30 pages, split the dump across reviewer subagents when the harness has them:**
   - When `pages > 30` and the harness provides subagents (`Agent` / `invoke_subagent`), partition `pending.jsonl` into batches (grouping candidates that share a `project`, `slug`, or `near` target into the same batch so duplicates stay together) and run reviewer subagents in parallel.
   - Give each subagent the full text of `$SKILL_DIR/BRIEF.md` and its batch of records. Subagents are strictly read-only on the repo and store and return draft decision objects only.
   - In the main session, combine the subagent decisions, fold any cross-batch duplicates with `covers`, and check that every `id` from `p1` to `pN` is covered once.

### Step 3: Mark `confidence: high|low` and settle low-confidence items yourself

1. Every decision must carry `"confidence": "high"` or `"confidence": "low"`.
2. For each draft decision marked `"confidence": "low"`, try to settle it yourself before asking the user:
   - Read the full candidate body and any existing pages in the project that touch the same topic (use `okfmem find <terms> --store "$STORE_PATH"` when `near` is empty).
   - Check the repository's source files, tests, and `CLAUDE.md` / `AGENTS.md` on disk.
   - If the pages or code settle whether the claim is true, duplicate, or obsolete, resolve the action and upgrade `"confidence"` to `"high"`.
3. Keep `"confidence": "low"` (with a one-sentence `"question"`) only for items that still require a human decision after those checks.

### Step 4: Ask the user only the remaining `confidence: low` items

- **Question ceiling:** never ask about `"confidence": "high"` decisions. On a mostly duplicate inbox where duplicates, drops, and merges are resolved at high confidence, ask **no more questions than there are remaining `"confidence": "low"` decisions** (and **0 questions** when no low-confidence decisions remain).
- When one or more `"confidence": "low"` decisions remain:
  - **When the harness has a question tool** (`AskUserQuestion` / `ask_question`): ask only those remaining low-confidence items as multiple-choice questions, putting the recommended choice first (`(Recommended) merge into <target>`, `keep as new <slug>`, or `drop`) and using the record's one-sentence `question`.
  - **When the harness has no question tool:** present only those remaining low-confidence items as a numbered text list with multiple-choice options (`[a] merge into <target> (recommended)`, `[b] new page <slug>`, `[c] drop`) and wait for the user's answer.
- Fold the user's answers into the decision records, set their `"confidence"` to `"high"`, and write the complete array to `<SCRATCH_DIR>/decisions.json` with the file-writing tool.

### Step 5: Show totals and new slugs, then run `okfmem inbox apply` (or offer `--web`)

1. Write `<SCRATCH_DIR>/decisions.json` with the file-writing tool.
2. Show the user a compact pre-apply summary:
   - **Totals:** `N drop / M merge into K page(s) / P new` (plus any in-place `replace` count and the number of duplicate pages folded via `covers`).
   - **New slugs:** each `"action": "new"` page's `<project>/<slug>` — `<description>` (or `"none"`).
   - **Merge targets:** each target page slug being updated (or `"none"`).
   - **Visual review alternative:** note that if the user would rather click through the decisions in a browser before applying, they can run:

     ```bash
     okfmem inbox review --web --store "$STORE_PATH"
     ```

3. If the user asks to review visually in the browser, run `okfmem inbox review --web --store "$STORE_PATH"` (forwarding `--into "$INTO_STORE"` when set) instead of CLI `apply`.
4. Otherwise run `okfmem inbox apply`:

   ```bash
   okfmem inbox apply --store "$STORE_PATH" "$SCRATCH_DIR/decisions.json"
   ```

   - `okfmem inbox apply` validates `<SCRATCH_DIR>/decisions.json` against `<SCRATCH_DIR>/pending.jsonl`, prints the plan, and uses its `[y/N]` prompt as the final confirmation gate.
   - If `okfmem inbox apply` reports validation problems (exit `1`), fix `<SCRATCH_DIR>/decisions.json` (for example, re-reading a merge target to fix a non-unique `replace.old`) and re-run `okfmem inbox apply`.
   - When the harness executes shell commands without a TTY, `okfmem inbox apply` without `--yes` writes nothing, prints the plan and the manual `--yes` command, and exits `2`. In that case, ask the user to confirm the Step 5 summary (or choose `okfmem inbox review --web`), and pass `--yes` to `okfmem inbox apply` **only after** the user explicitly confirms.

## Anti-patterns

- **Writing outside `okfmem inbox apply`.** Never write or edit files in `pages/`, `projects/`, `views/`, or `state/`, and never manipulate `refs/okfmem/inbox/*` or run `okfmem sync` directly from this skill.
- **Asking about high-confidence or duplicate items.** Duplicates folded via `covers`, run narration drops, and verified merges are `"confidence": "high"` and never generate user questions.
- **Choosing a merge target without reading it.** Always read the target page before setting `"action": "merge"` or `"replace"`.
- **Putting store-specific paths in `BRIEF.md`.** `skills/okfmem-inbox/BRIEF.md` is repository-shipped and store-agnostic.
