---
name: gaal-revise-pr
description: Revise an open pull request in 116-Labs/okfmem in response to review. Addresses every unresolved review thread with a code fix or a reasoned reply, runs the gates, pushes once per round, then replies to and resolves threads by rule and requests review again. Use when the dispatcher hands over a repo and PR number and asks for the revise-pr step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Feedback may also be imported from another PR, a single comment URL, or free-text notes. Do not use to implement an issue, open a PR, review a PR, or merge. Those belong to other steps. Always ends by writing `$GAAL_RUN_DIR/result.json`.
---
<!-- gaal-stamp blueprint=revise-pr@1.1.0 shared=1.0.0 profile=424ba91531b1fc7c generated=2026-09-29 content=d552093d804bdab7 -->

# gaal-revise-pr

Close the review loop on one open PR in `116-Labs/okfmem`. Every unresolved thread gets a code change or a reasoned reply. The branch is pushed once per round with green gates. Threads end in a state that matches what was done. Then review is requested again.

This skill runs headless. Never wait for input. Ambiguity ends the run as `needs-clarification`.

## Project facts

- Repo: `116-Labs/okfmem`. Base branch: `main`. Tracker: GitHub.
- Branch prefix for automation branches: `gaal/`.
- Required gate: `python3 -m pytest -q` (name `test`).
- Preflight before committing: `python3 scripts/check-leaks.py` (name `leaks`). It scans tracked file content for private strings and must exit 0.
- Commits: conventional style (`fix: ...`, `docs: ...`). Single commit per PR. Attribution policy is `pr-provenance`: commit messages carry no `Co-Authored-By:` trailer, no `Claude-Session:` trailer, no session URL and no generated-by badge (`attribution-policy`). Model provenance, if it belongs anywhere, goes only in the PR body as a short prose `## Provenance` block. This skill does not edit the PR body.
- Merge: squash through a merge queue. The squash message comes from the commits (`merge.message_source: commits`), so the branch must land as one well-written commit.
- Review: 1 approving review required. `threads_block_merge` is false. Open threads do not block merge, but they still forbid a collapse (`collapse-with-open-threads`).
- Limit: `limits.revise_rounds` = 3 gate-fix rounds.

## Steps

### 0. Start

Record `started_at` (UTC, ISO 8601 with `Z`). Keep a manifest of every path this run writes (`explicit-staging`). Track `rounds_used` from 0.

### 1. Identify and confirm the PR

Take the repo and PR number from dispatch. If no PR number can be determined, end as `needs-clarification` with a question. Never guess.

Fetch the PR state, head branch, head sha, head repository, base branch and commit authors with the GitHub CLI. If the call fails (auth, rate limit, network), stop with `failed` and name the call. An error is never "no PR" (`fail-closed-reads`).

- PR closed or merged: end as `failed`.
- Head branch equals `main`: refuse. Never commit or push to the base (`base-untouched`). End as `failed`.
- Head repository is a fork: the branch is not pushable from here. End as `needs-human`.

### 2. Check out the PR head as fetched

Fetch the head branch from the remote and check out exactly the fetched sha, on a local branch tracking it. Record `head_sha` (the inspected sha, needed later for leases). If the checkout has uncommitted changes this run did not make, stop as `needs-human`. Never commit them (`commit-foreign-edits`).

If the dependency lockfile differs from the last install, reinstall dependencies before running gates. The core is standard-library Python, so this is usually a no-op.

### 3. List every unresolved thread

Use the GitHub GraphQL API through `gh api graphql` on `pullRequest.reviewThreads`. It exposes `id`, `isResolved`, `isOutdated`, and comments. Paginate with `pageInfo.endCursor` until `hasNextPage` is false. Also paginate each thread's comments if `hasNextPage` is true there. If any page fails or pagination cannot complete, stop with `failed`. Truncated data is never trusted (`complete-listings`, `fail-closed-reads`).

Record every unresolved thread id, its comment ids and its author **now, before any history rewrite**. Rewriting history marks threads outdated, and the ids are needed for replies later. Tag each item with its source: `this-pr`, or `import`.

For imported feedback:
- From another PR (`--from-pr`): list that PR's unresolved threads the same way, paginated to the end.
- From a comment URL (`--from-comment`): check the URL's shape first. An issue-level comment (`issuecomment-…`) has no thread and no thread id. A review comment (`discussion_r…`) does. Extract ids only from the shape that has them.
- Free text (`--notes`): treat each distinct point as one item.

Fixes for imported items land only on this PR's branch (`target-branch-only`).

If there are no unresolved threads and no imports (and the listing succeeded), there is nothing to change. Skip to step 11 with `commit_sha` = `head_sha`, no push, and `gates` empty.

### 4. Re-verify imported items

For each imported item, check it against this branch and classify it: reproduces, partly applies, or does not apply. Apply only what reproduces here. Never apply an imported finding that does not reproduce (`apply-unreproduced-import`). An item that does not apply still gets a reply with the reason (`every-thread-answered`). Every unresolved thread at the start of the run must end with a reply, imports included.

### 5. Decide each item

Choose exactly one per item:

- **fix**: change code, tests or docs.
- **answer**: a question or explanation needs no change.
- **defer**: real but out of scope. File the follow-up issue first, and record the reviewer's agreement on the thread. A deferral with no issue number is a pushback in disguise.
- **push back**: disagree, with reasons.

If a reviewer's request is so ambiguous that any fix would be a guess, do not guess. End as `needs-clarification`, with `questions` quoting the thread.

### 6. Make all fixes

Make every fix in the working tree before running anything. Add each path written to the manifest. Do not fix piecemeal (`piecemeal-push`). Keep changes limited to what the threads ask.

### 7. Run preflight and gates

Each pass through this step is one round. Increment `rounds_used`.

1. Run `python3 scripts/check-leaks.py`. It must exit 0. On failure, fix the named `file:line` and re-run.
2. Run `python3 -m pytest -q`. Record name, exact command, exit code and duration for the result. Check the exit code directly. Never let a pipe or filter hide it (`status-preserved`).

On failure, fix and repeat, using another round. Never push a red tree to clear comments (`push-red`, `gates-green-before-push`). When `rounds_used` reaches 3 (`bounded-rounds`) and the gates are still red, end as `needs-human` with the failing gate in `reason`.

If a gate fails in tests this revision did not touch, check the base in a throwaway worktree. If it is red there too, end as `failed`, with `reason` reporting it as pre-existing. Tear the worktree down on every exit path.

A gate that did not run is absent from the result, not passed (`truthful-report`).

### 8. Commit once and push once

Stage only manifest paths by explicit path (`explicit-staging`). After staging, confirm `git status` shows no other changes this run is responsible for. Never stage everything wholesale and never use commit-all shortcuts (`commit-foreign-edits`).

Create one fixup commit with a conventional message, for example `fix: address review feedback`. No attribution lines (`attribution-policy`). Then push once, as a normal fast-forward push, to the PR branch only (`one-push-per-round`, `target-branch-only`):

```
git push origin HEAD:<pr-branch>
```

Check the exit status. If the push is rejected because of a conflict or permissions, end as `needs-human` and name it in `reason`. If it fails for any other reason, end as `failed`. Never force-push here.

Record the fixup commit sha and confirm the remote head equals it.

### 9. Collapse, only on the final round

Collapse only when all of these hold:
- every thread on this PR will be resolved after step 11's replies (no pushback, no unagreed deferral remains open), and
- `merge.message_source` is `commits`.

Otherwise keep the fixup commit separate so the reviewer can diff only the delta, and skip to step 10. Never collapse while any thread on this PR will stay open (`collapse-with-open-threads`).

The fixup push in step 8 must have landed before the collapse reads the remote head. Otherwise the collapse ships without the fix.

Apply the shared single-commit collapse routine in **PR mode** (never infer the mode). The collapse's lease push is the routine's own push, not a second fix push. Do not use an interactive rebase.

1. Regime: `message_source` is `commits`, so collapse applies. Idempotence: if the branch is zero or one commit ahead of the merge base, do nothing.
2. Read the head sha from the remote and check it equals the sha just pushed. Rewrite in a throwaway detached worktree.
3. Soft gates: an approval on the PR would be dismissed (treat an unreadable setting as yes), and unresolved threads exist (print the count). Knowingly override the unresolved-threads soft gate here, because the threads just fixed resolve after the push. State the override in the report.
4. Hard gates, never overridden: the branch lives on a fork, or any commit on it was authored by someone else; the remote head is not an ancestor of what will be pushed. If one trips, the collapse refuses and the run ends as `failed`, naming the gate.
5. Divergent local checkouts: back local-only commits up to a permanent branch ref, verify the backup, then reset. Uncommitted edits of unknown origin stop the collapse for a human (`needs-human`).
6. Write one message for the whole change, conventional style. Drop process commits ("wip", "fix lint") and follow the attribution policy (no trailers).
7. Content preservation: if the rewrite nets to an empty change, restore and stop. After committing, the tree hash must equal the pre-collapse tree hash, or abort before pushing.
8. Push with an explicit lease on the inspected sha:

```
git push --force-with-lease=<pr-branch>:<inspected-sha> origin <new-sha>:refs/heads/<pr-branch>
```

   On failure, restore the previous HEAD. Tear down only the throwaway worktree, on every exit path. A lease without an expected sha is forbidden (`bare-force-push`). Never merge as admin instead (`admin-bypass`).
9. Hard-reset any stale checkout to the remote. Never pull after a collapse (`pull-after-collapse`), since a pull merges the old history back in.

Record the collapse report: mode, regime, commit count before and after, gates that fired, tree hash, old and new sha, backup refs in full, and the reset each stale checkout needs. The new sha is now `commit_sha`.

### 10. Reply to every thread

Reply through the thread relation (the GraphQL `addPullRequestReviewThreadReply` mutation with the thread id captured in step 3). This works on outdated threads. Check each write's status (`status-preserved`). Every thread from step 3 gets a reply (`every-thread-answered`). Each reply must describe what was done (`reply-matches-action`):

- **fix**: name the sha that contains the fix. After a collapse, name the post-collapse sha. "Fixed in `<sha>`" appears only when that sha contains the fix (`truthful-report`).
- **answer**: state the explanation.
- **defer**: name the follow-up issue number. The issue is filed first, unassigned to any milestone, with labels that exist in the repo.
- **push back**: give the reasons.
- **import that did not apply**: give the reason it does not reproduce here.
- **import that was fixed**: say where the fix landed (`target-branch-only`).

### 11. Resolve threads by rule

Resolve a thread (GraphQL `resolveReviewThread`) only if it was fixed, is outdated, or was deferred with agreement recorded on the thread (`resolve-by-rule`).

- Leave pushbacks open for the reviewer (`resolve-pushback`).
- Leave deferrals that only the reviser decided on open (`resolve-unagreed-deferral`).
- Resolving another PR's thread may fail on permissions. The reply is what matters, so this is not a blocker. Note it in the report.

### 12. Request review again

Pushes can dismiss approvals, and one approving review is required. Re-request review from the reviewers who left the threads through GitHub, using the GitHub CLI. `review.reviewers` is empty in the profile, so there is no fixed reviewer list. If a request cannot be made (for example, the author is the requester), say so in the report. Do not merge. Do not enable auto-merge. Merging goes through the queue and belongs to people.

### 13. Write the run result (last step, every exit path)

Write it on every exit path, including failures, and including early exits from steps 1–9. If `GAAL_RUN_DIR` is set, write the JSON to a temp file in that directory, then rename it to `$GAAL_RUN_DIR/result.json` atomically (`run-result-written`). Keep the report of what happened (one row per item: source, decision, sha or issue; open pushbacks listed as normal; collapse report if any) in the final output, since the JSON schema has no field for it.

Fields (no extra keys):

```
{
  "schema_version": 1,
  "run_id": "<value of $GAAL_RUN_ID>",
  "blueprint": "revise-pr",
  "blueprint_version": "1.1.0",
  "repo": "116-Labs/okfmem",
  "issue": null,
  "pr": <PR number, or null if none was determined>,
  "status": "done" | "needs-human" | "needs-clarification" | "failed",
  "reason": "<required unless status is done>",
  "questions": ["<required, non-empty, when needs-clarification>"],
  "attempts": <rounds used, integer >= 1>,
  "gates": [ { "name": "test", "command": "python3 -m pytest -q", "exit_code": 0, "duration_ms": 0 } ],
  "branch": "<PR head branch, or null>",
  "commit_sha": "<40 hex chars, or null>",
  "started_at": "<UTC ISO 8601 with Z>",
  "finished_at": "<UTC ISO 8601 with Z>"
}
```

- Omit `reason` when `done`. Include a non-empty `reason` otherwise. Include `questions` only for `needs-clarification`.
- `attempts` is at least 1. Use 1 for a run that ended before any gate round.
- `gates` lists only gates that actually ran, with their real exit codes. It may be empty.
- `commit_sha` is the PR head after the run: the collapsed sha, the fixup sha, or the unchanged head when nothing was pushed. Use `null` only if it could not be determined.
- The report and result describe what actually happened (`truthful-report`).

## Exit states

- `done`: every thread is answered and the branch is pushed with green gates. When nothing needed changing, `commit_sha` is the unchanged head, nothing was pushed, and `gates` may be empty. Open pushbacks are normal and are listed in the report.
- `needs-clarification`: no PR could be determined, or a reviewer's request is ambiguous enough that any fix would be a guess. `questions` quote the thread.
- `needs-human`: the round limit (3) was reached with red gates; or a conflict or permission problem blocks the push; or the PR is on a fork; or uncommitted edits of unknown origin block a checkout or collapse. `reason` names it.
- `failed`: the PR is not open or the head is `main`; a read, listing or API write failed; the push failed for a reason other than conflict or permission; or the collapse refused. `reason` names the step. Gates already red on the base are reported as pre-existing.

## Invariants

- `every-thread-answered`: every unresolved thread at the start has a reply at the end, including imports that did not apply.
- `one-push-per-round`: the branch is pushed exactly once per round, after all fixes.
- `gates-green-before-push`: `python3 -m pytest -q` exited 0 on the tree that was pushed, and the leak preflight passed.
- `reply-matches-action`: each reply states what was done: a sha containing the fix, an issue number for a deferral, reasons for a pushback.
- `resolve-by-rule`: resolve only fixed, outdated, or agreed-deferral threads.
- `target-branch-only`: code changes land only on this PR's branch.
- `bounded-rounds`: gate-fix cycles stop at 3 rounds, ending as `needs-human`.
- `explicit-staging`: stage only manifest paths, by explicit path.
- `base-untouched`: never commit or push to `main`.
- `fail-closed-reads`: errors, auth expiry and rate limits stop the run. They never become "no PR" or "no threads".
- `complete-listings`: threads, reviews, comments and checks are paginated to the end or the run stops.
- `truthful-report`: the report and result describe what happened. A gate that did not run is absent.
- `status-preserved`: no pipe, filter or guard hides a failed push, gate or API write.
- `attribution-policy`: follow `pr-provenance`. Commits carry no attribution or session trailers.
- `run-result-written`: `result.json` is written atomically on every exit path.

## Forbidden actions

- `push-red`: pushing with a failing required gate to clear comments.
- `piecemeal-push`: pushing once per comment instead of once per round.
- `resolve-pushback`: resolving a thread where the revision disagreed with the reviewer.
- `resolve-unagreed-deferral`: resolving a deferral only the reviser decided on.
- `apply-unreproduced-import`: applying an imported finding that does not reproduce here.
- `collapse-with-open-threads`: collapsing while any thread on this PR will remain open.
- `pull-after-collapse`: pulling into a checkout after a collapse. Hard-reset it to the remote instead.
- `bare-force-push`: force-pushing without `--force-with-lease=<branch>:<sha>` on the inspected sha. Never use `--force` or `-f`.
- `admin-bypass`: merging, pushing or rewriting with admin privileges, or skipping the merge queue or verification hooks (never `--no-verify`, never `--admin`).
- `machine-specific-paths`: hard-coding a person's home directory, private scripts or services. Take everything from the profile.
- `commit-foreign-edits`: committing changes this run did not make. Never use `git add -A`, `git add .` or `git commit -a`.
