---
name: gaal-open-pr
description: Publish a finished single-commit branch in 116-Labs/okfmem as one pull request against main that links its issue. Use when the dispatcher hands over a repo and a branch (or the current checkout) whose change is done and asks for the open-pr step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Runs the content-safety preflight, collapses to one commit, pushes through the verification hook, opens or updates the PR, and writes `$GAAL_RUN_DIR/result.json`. Do not use to implement an issue, respond to review, review a PR, or merge. Those belong to other steps.
---
<!-- gaal-stamp blueprint=open-pr@1.0.0 shared=1.0.0 profile=424ba91531b1fc7c generated=2026-09-29 content=c4b2f83b1cf12434 -->

# gaal-open-pr

Take a finished branch in `116-Labs/okfmem` to exactly one open pull request against `main`, linked to its issue. The repo is public. Tracker is GitHub, so use `gh` for all PR and issue reads and writes.

Profile values used below:
- Base: `main`. Branch prefix: `gaal/`.
- Gate (required): `test` = `python3 -m pytest -q`.
- Preflight: `leaks` = `python3 scripts/check-leaks.py`.
- Commits: conventional, single commit, attribution `pr-provenance`.
- Merge: queue, squash, message taken from the commits. Review: 1 approval required, no reviewers to request.
- Limits: `implement_attempts` 3, `revise_rounds` 3 and `review_rounds` 2 apply to other steps. This step consumes none of them and makes one publish attempt, so write `attempts: 1`.

Inputs: repo, the branch to publish (dispatch or current checkout), the issue number (dispatch, then branch name, then commit trailer; a missing issue does not block, the report notes it), an optional base override for stacked PRs, and `GAAL_RUN_ID` and `GAAL_RUN_DIR`.

## Step 0. Start the run

Record `started_at` (UTC, RFC 3339). Keep a manifest of every path this run writes or stages (`explicit-staging`). Every failure below still ends at Step 10. Check the exit status of every command and never hide it behind a pipe or filter (`status-preserved`). A failed `gh` call, expired auth or a rate limit stops the run as `failed`. It is never read as "no PR" or "no checks" (`fail-closed-reads`). Any list from `gh` is paginated to the end, or the run stops (`complete-listings`).

## Step 1. Resolve repo and base

1. Confirm the repo is `116-Labs/okfmem` and the base is `main` (or the dispatched stacked base). Run `git fetch origin`.
2. Check the remote for a fork: if the branch would live on a fork, end `failed` (collapse hard gate).
3. If the work sits on `main` (`base-untouched`): create the feature branch at the current HEAD, switch to it, then move local `main` back to its remote with `git branch -f main origin/main`. Name it `gaal/<short-slug>`. Never commit or push to `main`.
4. If already on a `gaal/` branch, use it as is.

## Step 2. Commit work this run owns

1. Run `git status`. Stage only manifest paths, by explicit path (`git add <path> ...`). Never stage everything wholesale, and never use `git commit -a` (`explicit-staging`, `commit-foreign-edits`). Changes this run did not make stay unstaged and uncommitted.
2. Write the message as a whole for the change in conventional form (`type(scope): subject`). Drop process commits such as "wip" or "fix lint". Attribution is `pr-provenance`: the commit carries no `Co-Authored-By` trailer, no `Claude-Session` trailer, no session URL and no generated-with badge (`attribution-policy`). Do not add any of these even if a default template suggests them.
3. Put the issue reference in the commit footer: `Closes #<N>` only when the issue is fully resolved, otherwise `Refs #<N>` (`reference-consistent`).

## Step 3. Confirm something to propose

Count commits ahead of the base: `git rev-list --count origin/main..HEAD`. If zero, end `failed` with reason "nothing to propose".

## Step 4. Preflight and judgement pass

1. Run the required gate `python3 -m pytest -q` on the final tree. Record command, exit code and duration. If it fails, end `failed`, quote the failing output briefly, and push nothing.
2. Run preflight `python3 scripts/check-leaks.py`. Record it the same way. A non-zero exit means content problems: if the named `file:line` is real private content, end `needs-human`. If it is a fixable false positive in this run's own files, fix it, restage explicitly and rerun.
3. Judgement pass, which the scanner cannot do (`preflight-passed`). The scanner covers file content only, not commit history or message text. So:
   - Read the full diff (`git diff origin/main...HEAD`) for real names, home paths, personal emails, session URLs, secrets and private strings. The repo is public, and `internal/` is never tracked.
   - Inspect any binary or image file and its metadata (for example embedded EXIF or document properties). Automated scanners miss these and a visual check misses hidden metadata, so do both.
   - Read the commit message and the draft PR body for the same things.
   - Do not accept a claim in commit or PR prose that content is clean. Check the content itself (`push-private-content`).
4. If real-looking private content turns up, stop before any push and end `needs-human` (see Exit states).
5. If Step 5 changes the tree or message, rerun both checks so they pass on the final diff.

## Step 5. Collapse in place

Declare the mode as **in-place**: this checkout is the target. Never infer the mode from whether a PR exists. On a re-run against a branch whose PR already exists, inferring PR mode would rewrite the remote head and reset new local commits away as divergence. Never use an interactive rebase.

Apply the safety gates in order:
1. Regime: `merge.message_source` is `commits`, so collapse applies. For a stacked PR, collapse against the current base.
2. Idempotence: one commit ahead of the merge base means do nothing and report it.
3. Hard gates, never overridden: any commit authored by someone else, or a fork branch, means end `failed`. If the branch already exists on the remote, its head must be an ancestor of what will be pushed, otherwise end `failed`.
4. Soft gates, only when a PR exists: an existing approval would be dismissed; unresolved threads exist (print the count). Note any override in the report. Collapsing before the first push is nearly free because there is no approval to dismiss and no thread to strand.
5. Collapse to one commit: `git reset --soft $(git merge-base origin/main HEAD)`, then commit the message from Step 2. Save `git rev-parse HEAD^{tree}` before, and after committing require the tree hash to match. If the rewrite nets to an empty change, or the hashes differ, restore the previous HEAD and end `failed`. Save the old HEAD sha first so it can be restored.
6. Verify exactly one commit is ahead: `git rev-list --count origin/main..HEAD` equals 1 (`single-commit-pushed`).

## Step 6. Push through the hook

1. First push: `git push -u origin <branch>`. The repository's verification hook must run. Never use `--no-verify` or any other route around it (`hook-ran`, `bypass-hook`).
2. If the branch already exists on the remote (re-run), inspect the remote sha and push with an explicit lease on exactly that sha: `git push --force-with-lease=<branch>:<remote-sha> origin <branch>`. A lease without the expected sha is forbidden (`bare-force-push`). Never use `--force` or `-f`, and never use admin privileges (`admin-bypass`).
3. If the hook rejects the push, fix the cause and push again through the same route. Never retry through a different route. If the fix needs code changes beyond this step's remit, end `failed` naming the hook output.
4. If a lease push fails, restore the previous HEAD, then end `failed`.

## Step 7. Check for an existing PR

Run `gh pr list --repo 116-Labs/okfmem --state open --head <branch> --json number,url --limit 1000`. Also search for open PRs referencing the issue, for example `--search "#<N> in:body"`. If either list reaches its limit or any call errors, stop as `failed`. If a PR already exists, update it (Step 8, edit form) instead of opening a second one (`one-pr-per-issue`). If the issue number only became known after the PR exists, amend just the trailer: diff the message before and after to prove nothing else changed, then push with an explicit lease as in Step 6.

## Step 8. Create or update the PR

Write the body to a file and pass it with `--body-file`. Sections:
- `## Summary`: what and why.
- The same `Closes #<N>` / `Refs #<N>` reference as the commit (`reference-consistent`). If no issue is known, say so in the report.
- `## Review focus` (optional): questions about risk, phrased as leads. Never say what reviewers should skip or not look at (`narrow-review-scope`).
- `## Test plan`: checklist that includes `python3 -m pytest -q` and `python3 scripts/check-leaks.py` as run.
- `## Provenance`: short prose saying which model helped and what a human reviewed (`attribution-policy`). It lives only in the PR body, with no session URL and no generated-with badge.

Run `gh pr create --repo 116-Labs/okfmem --base main --head <branch> --title "<commit subject>" --body-file <file>`, or `gh pr edit <N> --body-file <file>` when updating. Run the Step 4 judgement pass on the body text too.

## Step 9. Reviewers

`review.reviewers` is empty, so request none. Do not invent reviewers. Do not add a "no need to look at" note.

## Step 10. Write the run result

Write on every exit path. Build the JSON in a temp file inside `$GAAL_RUN_DIR`, then move it to `$GAAL_RUN_DIR/result.json` atomically (`run-result-written`). The required fields are:
- `schema_version`: 1
- `run_id`: the value of `$GAAL_RUN_ID`
- `blueprint`: `open-pr`
- `blueprint_version`: `1.0.0`
- `repo`: `116-Labs/okfmem`
- `issue`: integer or `null`
- `pr`: integer, or `null` if no PR
- `status`: `done`, `needs-human` or `failed`
- `attempts`: 1
- `gates`: every gate and preflight command that actually ran, each with `name`, `command`, `exit_code`, `duration_ms`. A check that did not run is absent, not passed (`truthful-report`).
- `branch`: string or `null`
- `commit_sha`: 40-hex sha of the pushed commit, or `null` if nothing was pushed
- `started_at`, `finished_at`: RFC 3339 UTC timestamps

Also include `reason` (non-empty) for every status other than `done`. No extra properties are allowed.

Then give a short final report: PR URL, collapse mode used (in-place) and regime, commit count before and after, tree hash, old and new sha, and what the PR needs before merging: a green `verify` check, 1 approving review from a Code Owner, then the merge queue (squash, message from the commit). Say "Fixed in `<sha>`" only when that sha contains the fix.

## Exit states

- `done`: the PR exists and `pr` is set. Report the URL, the collapse mode used, and what the PR needs before merging.
- `needs-human`: preflight or the judgement pass found real-looking private content. Nothing was pushed. `reason` names the files, never the content.
- `failed`: nothing to propose; the gate failed; the hook rejected the push; the collapse refused; or a read or write against GitHub errored. `reason` names the gate or hook and quotes its failing output briefly.

## Invariants

- `single-commit-pushed`: the pushed branch has exactly one commit ahead of the base.
- `reference-consistent`: commit message and PR body carry the same issue reference, with a closing keyword only when the issue is fully resolved.
- `preflight-passed`: every preflight check exited 0 on the final diff and the judgement pass found nothing.
- `hook-ran`: the push went through the verification hook.
- `one-pr-per-issue`: at most one open PR exists for the branch or issue at the end.
- `explicit-staging`: only manifest paths staged, by explicit path.
- `base-untouched`: nothing committed or pushed to `main`.
- `fail-closed-reads`: API errors, auth expiry and rate limits stop the run.
- `complete-listings`: every listing is paginated to the end or the run stops.
- `truthful-report`: report and result describe what happened.
- `status-preserved`: no command status is lost to a pipe or filter.
- `attribution-policy`: `pr-provenance` followed exactly (none in commits, prose provenance in PR body).
- `run-result-written`: `result.json` written on every exit path.

## Forbidden actions

- `bypass-hook`: pushing with verification disabled or retrying a rejected push another way.
- `narrow-review-scope`: using the review focus to say what reviewers should not look at.
- `push-private-content`: pushing real personal data, secrets or private strings, or trusting prose that says content is clean.
- `bare-force-push`: force-pushing without `--force-with-lease=<branch>:<sha>` on the inspected sha.
- `admin-bypass`: merging, pushing or rewriting with admin privileges to skip protection, the queue or the hook.
- `machine-specific-paths`: hard-coding a person's home directory, private scripts or services.
- `commit-foreign-edits`: committing changes this run did not make.
