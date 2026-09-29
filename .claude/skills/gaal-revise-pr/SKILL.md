---
name: gaal-revise-pr
description: Revise an open pull request in 116-Labs/okfmem in response to review. Addresses every unresolved review thread with a code fix or a reasoned reply, runs the gates, pushes once per round, then replies to and resolves threads by rule and requests review again. Use when the dispatcher hands over a repo and PR number and asks for the revise-pr step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Feedback may also be imported from another PR, a single comment URL, or free-text notes. Do not use to implement an issue, open a PR, review a PR, or merge. Those belong to other steps. Always ends by writing `$GAAL_RUN_DIR/result.json`.
---
<!-- gaal-stamp blueprint=revise-pr@1.1.0 shared=1.1.0 profile=424ba91531b1fc7c generated=2026-09-29 content=ab17f15d9405a178 -->

# gaal-revise-pr

Close the review loop on one open PR in `116-Labs/okfmem`. Every unresolved thread gets a code change or a reasoned reply. The branch is pushed once per round with green gates. Threads end in a state that matches what was actually done. Then review is requested again.

Blueprint: `revise-pr` version 1.1.0.

## Repository facts

- Repo: `116-Labs/okfmem`, base branch `main`, public repository, tracker is GitHub.
- Gate (required): `python3 -m pytest -q`
- Preflight (content safety, run before every commit): `python3 scripts/check-leaks.py`
- Commits: one commit per PR (`single_commit: true`), conventional commit messages, attribution policy `pr-provenance`. Commit messages carry no model or session attribution: no `Co-Authored-By:` trailer, no session URL, no "Generated with" badge. Provenance, if it needs updating, is a factual `## Provenance` section in the PR body only.
- Branch prefix for agent work: `gaal/`. The PR's own branch is whatever it is; only that branch is touched.
- Merge: merge queue, squash, message derived from the commits (`message_source: commits`).
- Review: 1 approving review required. `threads_block_merge` is false. `reviewers` is empty. `start_signal` is `reaction`.
- Limit: `limits.revise_rounds` = 3.
- Never use `--no-verify`, `--admin`, `--force`, `-f`, `git add -A`, `git add .` or `git commit -a`.

## Inputs

- Repo and PR number from dispatch. If no PR can be determined, stop as `needs-clarification`. Never guess a PR.
- Optional imported feedback: threads from another PR (`--from-pr`), a single comment URL (`--from-comment`), or free-text notes (`--notes`). Each import is re-verified against this branch before any change.
- `GAAL_RUN_ID` and `GAAL_RUN_DIR`. Record the start time (UTC, RFC 3339) for `started_at`.

## Steps

### 1. Confirm the PR is open

Read the PR state. Distinguish "the API said closed/merged" from "the call failed" (`fail-closed-reads`): an API error, auth expiry or rate limit stops the run as `failed` or `needs-human`, and is never treated as "no PR". If the PR is closed or merged, end as `failed`. Record the PR head branch, head sha, and whether the head repository is a fork. Record the linked issue number if the PR body names one (else `null`).

### 2. Check out the PR head

Fetch the PR head from the remote and check out exactly that sha on a local branch tracking it (`base-untouched`: never commit or push to `main`; if the checkout is on `main`, move to the PR branch first). The project has no dependency lockfile step named in the profile, so nothing is installed. Start a manifest of paths this run writes (`explicit-staging`). If the checkout has uncommitted changes this run did not make, stop as `needs-human`. Never commit them (`commit-foreign-edits`).

### 3. List every unresolved review thread

Use an API that exposes resolution state and thread ids (for example the GitHub GraphQL `reviewThreads` connection through `gh api graphql`). Paginate to the end; if pagination cannot complete, stop (`complete-listings`, `fail-closed-reads`). Record each thread id, its first comment, outdated flag and path before any history rewrite, because rewriting history marks threads outdated. Tag each item with its source: this PR or an import. Also note the total count of unresolved threads; this is the set that `every-thread-answered` covers.

Import handling:
- `--from-comment <url>`: check the URL's shape first. An issue-level comment (conversation comment) has no thread and no thread id; treat it as free text and answer it with a PR comment. Only review comments belong to threads.
- `--from-pr`: list the other PR's unresolved threads the same way, paginated, and tag them as imports.
- `--notes`: each distinct request becomes one item.

If a reviewer's request is so ambiguous that any fix would be a guess, do not guess; collect a question quoting the thread and continue with the rest. If anything is left unresolvable this way, the run ends `needs-clarification` at step 13, after all other threads are handled.

### 4. Re-verify imported items

For every imported item, reproduce it on this branch. Classify as reproduces, partly applies, or does not apply. Do not apply a finding that does not reproduce (`apply-unreproduced-import`). An import that does not apply still gets a reply with the reason (`every-thread-answered`).

### 5. Decide each item

Choose one per item:
- **fix**: change the code.
- **answer**: a question or explanation; no code change.
- **defer**: real but out of scope. File a follow-up issue first (in GitHub, unassigned, no milestone) and record agreement on the thread. A deferral with no issue number is a pushback in disguise (`resolve-unagreed-deferral`).
- **push back**: disagree, with reasons.

Domain check for this repository: fixes must not introduce a real home path, a private session URL, a `Claude-Session:` trailer, a personal email, or anything from `internal/`. Never let a state-changing change skip the confirmation ladder described in `CLAUDE.md` (rung-2 needs `[y/N]`, rung-3 needs typed confirmation, and every prompt must be skippable non-interactively with the manual command printed).

### 6. Make all fixes

Apply every **fix** in the working tree, only on this PR's branch (`target-branch-only`). A fix for an imported item that belongs to another PR's branch is still applied here only if it reproduces here; the reply names where it landed. Add each written path to the manifest.

### 7. Run the gates

Run the preflight `python3 scripts/check-leaks.py` and the required gate `python3 -m pytest -q`. Capture each command's exit code and duration; never let a pipe or filter mask an exit status (`status-preserved`). On failure, fix and re-run; each fix-and-rerun cycle counts as one round against `limits.revise_rounds` (3) (`bounded-rounds`). If gates are still red when the limit is reached, end as `needs-human` and do not push (`push-red`, `gates-green-before-push`). If a gate was already red on the base before any of this run's changes, report it as pre-existing in the final message. Only a green tree gets pushed.

Skip this step and step 8 entirely if no code changes were made; then `commit_sha` is the unchanged head and `gates` may be empty.

### 8. Commit once and push once

Stage only the manifest paths, by explicit path (`explicit-staging`). Confirm afterwards that no other changes this run is responsible for remain in the working tree. Commit the fixes as one fixup commit with a conventional message (for example `fix: address review feedback on <topic>`), with no attribution trailer (`attribution-policy`). Push exactly once for the round, after all fixes (`one-push-per-round`, `piecemeal-push`): a plain fast-forward push of the branch to its remote. Check the push exit status; on failure end as `failed`, or as `needs-human` if a conflict or permission problem blocks it. Record the pushed sha.

### 9. Collapse only if every thread will be resolved

Collapse to one commit only when both hold: `merge.message_source` is `commits`, and no thread on this PR will remain open after step 11. Pushbacks and unagreed deferrals stay open, so if any exist, do not collapse; keep the fixup commit separate so the reviewer can diff only the delta (`collapse-with-open-threads`). The fix push in step 8 must already be on the remote before the collapse reads the remote head, or the collapse would ship without the fix.

When collapsing, run the shared routine in PR mode:
1. Read the head sha from the remote and check it equals the sha just pushed. Rewrite in a throwaway detached worktree, never in the current checkout.
2. Hard gates, never overridden: stop and end `failed` if the branch lives on a fork, if any commit was authored by someone else, or if the remote head is not an ancestor of what will be pushed. Zero or one commit ahead of the merge base is a no-op success.
3. Soft gates: an existing approval would be dismissed (an unreadable setting counts as yes), and unresolved threads exist. Knowingly override the unresolved-threads gate, because the threads just fixed resolve after the push; print the count and say so in the report.
4. If the local checkout has local-only commits, back them up to a permanent branch ref, verify it, then reset. Uncommitted edits of unknown origin stop the collapse for a human (`needs-human`).
5. Write one whole conventional commit message for the change. Drop process commits ("wip", "fix lint"). No attribution trailers.
6. Verify the tree hash after committing equals the pre-collapse tree hash; otherwise abort before pushing. A rewrite that nets to an empty change is restored and stopped.
7. Push with an explicit lease on the inspected sha, for example `git push --force-with-lease=<branch>:<inspected-sha> origin HEAD:<branch>`. Never a bare force push (`bare-force-push`) and never an admin bypass (`admin-bypass`). On failure restore the previous HEAD. Tear down only the throwaway worktree, on every exit path.
8. After a collapse, any stale checkout is hard-reset to the remote, never pulled (`pull-after-collapse`). Do not run `git pull` in this checkout afterwards. Report mode, regime, commit count before and after, tree hash, old and new sha, backup refs in full, and the reset each stale checkout needs. If the collapse refuses, end as `failed` with the refused gate.

Never use an interactive rebase.

### 10. Reply to every thread

Reply to every thread from step 3 through its thread reply relation (for example the `addPullRequestReviewThreadReply` mutation with the recorded thread id), which works on outdated threads (`every-thread-answered`). Replies must match the action (`reply-matches-action`):
- fix: name the sha that contains the fix. After a collapse, that is the new collapsed sha. Say "fixed in `<sha>`" only when that sha contains the fix (`truthful-report`).
- answer: give the explanation.
- defer: name the follow-up issue number, filed before replying, and ask the reviewer to confirm.
- push back: give the reasons.
- import that did not apply: give the reason and the branch evidence.
- import fix that landed elsewhere: name where it landed.

Issue-level comments with no thread are answered with a PR comment.

### 11. Resolve threads by rule

Resolve a thread only if it was fixed, is outdated, or was deferred with agreement recorded on the thread (`resolve-by-rule`). Leave pushbacks open (`resolve-pushback`) and leave deferrals without recorded agreement open (`resolve-unagreed-deferral`). Since `threads_block_merge` is false, open threads do not block the merge queue, but they are still owed a response from the reviewer. Resolving another PR's thread may fail on permissions; the reply is what matters, so record the failure in the report and continue. Do not treat it as a blocker.

### 12. Request review again

`review.reviewers` is empty and `start_signal` is `reaction`, so there is no reviewer list to re-request from and no other mechanism the profile names. Post one PR comment (comms.github_comments is on) stating that the round is pushed, at which sha, which threads were fixed, deferred or pushed back, and that the PR is ready for another look. Do not add attribution text beyond what `attribution-policy` allows.

### 13. Write the run result

Write `$GAAL_RUN_DIR/result.json` atomically (write to a temporary file in the same directory, then rename) on every exit path, including failures (`run-result-written`). Use this shape:

```json
{
  "schema_version": 1,
  "run_id": "<value of $GAAL_RUN_ID>",
  "blueprint": "revise-pr",
  "blueprint_version": "1.1.0",
  "repo": "116-Labs/okfmem",
  "issue": null,
  "pr": 0,
  "status": "done",
  "attempts": 1,
  "gates": [
    { "name": "test", "command": "python3 -m pytest -q", "exit_code": 0, "duration_ms": 0 }
  ],
  "branch": "<pr head branch>",
  "commit_sha": "<40-hex PR head after the run>",
  "started_at": "<RFC 3339 UTC>",
  "finished_at": "<RFC 3339 UTC>"
}
```

Rules for the values:
- `run_id` comes from `$GAAL_RUN_ID`. `issue` is the linked issue number or `null`. `pr` is the PR number, or `null` if none could be determined. `branch` and `commit_sha` are `null` when unknown; otherwise `commit_sha` is the full 40-character sha of the PR head after the run (the unchanged head when nothing was pushed).
- `attempts` is the number of rounds used, at least 1, at most 3.
- `gates` lists only gates that actually ran, each with its exact command, exit code and duration in milliseconds. A gate that did not run is absent, not passed (`truthful-report`). The preflight is not a profile gate; list it only if it is recorded as one, otherwise mention it in the final message.
- Unless `status` is `done`, include `reason`: one sentence, at most 160 characters, naming the decision or action needed. Detail goes in the PR comment and the final message.
- For `needs-clarification`, include `questions`: a non-empty array of strings, each quoting the thread it concerns.
- Include no extra fields.

The final message reports one row per item (source, decision, sha or issue), open pushbacks, any collapse report, and pre-existing red gates.

## Exit states

- `done`: Every thread is answered and the branch is pushed with green gates. `commit_sha` is the PR head after the run. When nothing needed changing, it is the unchanged head, nothing was pushed, and `gates` may be empty. Open pushbacks are normal and are listed.
- `needs-clarification`: No PR could be determined, or a reviewer's request is ambiguous enough that any fix would be a guess. `questions` quote the thread.
- `needs-human`: The round limit (3) was reached with red gates, or a conflict or permission problem blocks the push, or uncommitted edits of unknown origin block a collapse. `reason` names it.
- `failed`: The PR is not open, the push failed, or the collapse refused (fork, foreign author, non-ancestor head, tree mismatch). `reason` names the step.

## Invariants

- `every-thread-answered`: every thread unresolved at the start has a reply at the end, including imports that did not apply.
- `one-push-per-round`: one push per round, after all fixes.
- `gates-green-before-push`: every required gate exited 0 on the tree that was pushed.
- `reply-matches-action`: each reply states what was done: a sha containing the fix, an issue number for a deferral, reasons for a pushback.
- `resolve-by-rule`: resolve only fixed, outdated, or agreed-deferral threads.
- `target-branch-only`: code changes land only on this PR's branch; imports name where they landed.
- `bounded-rounds`: gate-fix cycles stop at 3 and end as `needs-human`.
- `explicit-staging`: stage only manifest paths, by explicit path.
- `base-untouched`: never commit or push to `main`.
- `fail-closed-reads`: API errors, auth expiry and rate limits stop the run; they never become "no threads" or "no PR".
- `complete-listings`: every listing is paginated to the end or the run stops.
- `truthful-report`: the report and result describe what actually happened.
- `status-preserved`: no exit status is lost to a pipe, filter or guard.
- `attribution-policy`: follow `pr-provenance` exactly; no attribution in commits, factual provenance only in the PR body, never claiming a person reviewed or approved.
- `run-result-written`: `result.json` is written on every exit path.

## Forbidden actions

- `push-red`: pushing with a failing required gate to clear comments.
- `piecemeal-push`: pushing once per comment instead of once per round.
- `resolve-pushback`: resolving a thread where the revision disagreed with the reviewer.
- `resolve-unagreed-deferral`: resolving a deferral only the reviser decided on.
- `apply-unreproduced-import`: applying an imported finding that does not reproduce on this branch.
- `collapse-with-open-threads`: collapsing while any thread on this PR will remain open.
- `pull-after-collapse`: pulling into a checkout after a collapse; hard-reset it to the remote instead.
- `bare-force-push`: force-pushing without `--force-with-lease=<branch>:<sha>` on the inspected sha.
- `admin-bypass`: merging, pushing or rewriting with admin privileges to get around protection, the merge queue or a verification hook.
- `machine-specific-paths`: hard-coding a person's home directory, private scripts or services.
- `commit-foreign-edits`: committing changes this run did not make.
