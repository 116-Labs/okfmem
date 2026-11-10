# okfmem Inbox Triage Brief

Use this brief to judge candidate records from `pending.jsonl` and produce a `decisions.json` array for `okfmem inbox apply`.

**Hard boundary:** never write memory pages or git refs directly. Output only decision objects in `decisions.json`.

## 1. Input record (`pending.jsonl`)

Each line of `pending.jsonl` is one candidate page:

- `id`: dump-local identifier (`"p1"`, `"p2"`, …). Every dumped `id` must be covered once in `decisions.json`.
- `project`: resolved project name, or `null` when the push's `repo` matches no registered project.
- `slug`: candidate page slug (filename without `.md`).
- `text`: full OKF markdown (YAML frontmatter `name`, `description`, `type`, optional `labels`, and body).
- `near`: closest existing pages in the destination project, each `{"slug", "why", "description"}` where `why` is `"slug"`, `"name"`, or `"description"`.
- `leaks`: secret or private-string pattern labels matched in `text`.
- `meta`: push metadata (`source`, `repo`, `ref`, `machine`, `time`).

## 2. What to keep, merge, or drop

- **Keep only durable, non-obvious, still-true facts:** engineering invariants, non-obvious traps and gotchas, reusable patterns, or standing workflow preferences (`type: feedback` / `type: user`) that cannot be reconstructed in one command from `git log`, `gh pr view`, the issue tracker, or reading the obvious source file.
- **Prefer `merge` into an existing page over `new`:** when an existing page (in `near` or in the project's live pages) already covers the surface or topic, fold the candidate's net-new, still-true facts into that page (`"action": "merge"`, `"target": "<existing-slug>"`).
  - **Read every merge target in full before choosing it.** Never guess a target's contents from its slug or one-line `description`.
  - Put only net-new markdown in `add` (do not repeat what the target already states). When the target already covers everything in the candidate, either use `"action": "drop"` or `"action": "merge"` with an empty/omitted `add`.
  - When the candidate shows a specific statement in the target is stale or inaccurate, include `"replace": [{"old": "<exact existing text>", "new": "<corrected text>"}]`. `old` must match the target file verbatim and appear in it **exactly once**.
- **Fold duplicate candidates with `covers`:** when two or more candidates in the dump teach the same lesson (or are dropped/merged/created together for the same topic), pick one primary `id` and list the duplicate ids in `"covers": ["p13", "p18"]`. Every dumped `id` must appear once across all `id` and `covers` fields.
- **Create a `new` page only when no existing page fits:** provide `"page": {"slug": "<kebab-slug>", "name": "<Title>", "description": "<one-line hook>", "type": "<user|feedback|project|reference>", "labels": [...]}` and optional cleaned body in `"add"`. Keep the compiled pointer line (`- [<name>](<slug>.md) — <description>`) within 150 characters, and pick a `slug` that does not already exist in the project.
- **Drop (`"action": "drop"`) without hesitation:**
  - Run narration and status wrappers ("PR #N merged", "issue #N closed", "CI passed after rebase", branch/worktree notes, commit SHAs, test-run transcripts).
  - Anything already covered by an existing page or by the repository's `CLAUDE.md` / `AGENTS.md`.
  - Obvious code facts that a quick grep or file read shows immediately.
  - Stale claims that no longer match the repository on disk.
  - Any candidate with non-empty `leaks`, unless `new`/`merge` rewrites the text so no secret or private string remains (when in doubt, drop).
  - Any candidate whose `project` is `null` (unresolved repository).

## 3. Spot-check code claims against the repository

When the project's repository checkout is on disk:

- Verify concrete code claims (file paths, function/class names, CLI flags, constants, and invariants) against the current tree before keeping or merging them.
- Read the repository's `CLAUDE.md` / `AGENTS.md` so candidates that merely repeat documented project rules are dropped.
- If the code has moved on and the candidate describes superseded behavior, mark `"action": "drop"` with `"confidence": "high"`.

## 4. Confidence (`high` vs `low`) and self-resolution

Every decision record must set `"confidence": "high"` or `"confidence": "low"`:

- **Settle uncertainty yourself first.** Before marking any item `"confidence": "low"`, read the candidate, read the relevant existing pages, and check the repository code on disk. If the pages or code answer the question, mark `"confidence": "high"`.
- **Duplicates, run narration, verified code facts, and clean merges into existing pages are `"confidence": "high"`.** Never mark a routine duplicate or a verifiable code claim `"low"`.
- **Mark `"confidence": "low"` only when a real human judgment call remains after checking pages and code**, such as:
  - a candidate and an existing page (or two candidates) contradict each other on policy and the code on disk does not settle which rule wins;
  - a candidate proposes a new user/team preference (`type: feedback` / `type: user`) that is a policy choice rather than a code fact;
  - a candidate makes a load-bearing claim about an external environment that cannot be checked from the repository or existing pages.
- Every `"confidence": "low"` record must include a specific one-sentence `"question"` naming the choice the user needs to make, alongside the recommended provisional `action`.

## 5. Output schema (`decisions.json`)

`decisions.json` is a JSON array of decision objects:

```json
[
  {
    "id": "p12",
    "action": "merge",
    "target": "existing-slug",
    "add": "Markdown paragraph with only the net-new fact.",
    "replace": [{"old": "exact stale line in target", "new": "updated line"}],
    "covers": ["p13", "p18"],
    "confidence": "high",
    "reason": "folds two duplicate pushes into existing rule"
  },
  {
    "id": "p14",
    "action": "new",
    "page": {
      "slug": "new-slug",
      "name": "Short title",
      "description": "One-line recall hook",
      "type": "feedback",
      "labels": ["topic:ci"]
    },
    "add": "# Short title\n\nCleaned durable lesson.",
    "confidence": "low",
    "question": "Keep this CI runner rule as a standing feedback page, or drop it as one-off?",
    "reason": "new workflow preference"
  },
  {
    "id": "p15",
    "action": "drop",
    "confidence": "high",
    "reason": "run narration already in git log"
  }
]
```

### Validation invariants enforced by `okfmem inbox apply`

- Every dumped `id` (`p1`..`pN`) is covered **exactly once** (as `id` or inside `covers`).
- All pages in `[id, *covers]` for a `new` or `merge` record resolve to the same non-null `project`.
- `action` is `"drop"`, `"merge"`, or `"new"`; `confidence` is `"high"` or `"low"`; `reason` is ≤ 15 words.
- For `"merge"`, `target` is an existing page slug in that project or a slug created by a `"new"` record in the same file, and every `replace` has non-empty, distinct `old` and `new` strings where `old` appears in `target` once.
- For `"new"`, `page.slug` is a valid page slug that does not already exist in the project, and `page.type` matches `^[a-z][a-z0-9_-]*$`.
- No text written by `"new"` or `"merge"` triggers the leak gate.
