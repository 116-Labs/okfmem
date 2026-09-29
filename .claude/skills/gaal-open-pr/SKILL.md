---
name: gaal-open-pr
description: Publish a finished single-commit branch in 116-Labs/okfmem as one pull request against main that links its issue. Use when the dispatcher hands over a repo and a branch (or the current checkout) whose change is done and asks for the open-pr step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Runs the content-safety preflight, collapses to one commit, pushes through the verification hook, opens or updates the PR, and writes `$GAAL_RUN_DIR/result.json`. Do not use to implement an issue, respond to review, review a PR, or merge. Those belong to other steps.
---
<!-- gaal-stamp blueprint=open-pr@1.0.0 shared=1.1.0 profile=424ba91531b1fc7c generated=2026-09-29 content=f8d33e09d3745d5b -->

# gaal-open-pr

Publish a finished change as one pull request against `main` in `116-Labs/okfmem`. The pushed branch holds one hand-written commit, and the PR body links the issue. The repository is public, so nothing private may leave the machine.

Repository facts, from the project profile:

- Repo `116-Labs/okfmem`, default branch `main`, visibility public, tracker GitHub.
- Required gate `test`: `python3 -m pytest -q`.
- Preflight `leaks`: `python3 scripts/check-leaks.py`.
- Branch prefix `gaal/`. Commits are conventional (`type(scope): subject`) and single-commit.
- Attribution policy `pr-provenance`: commit messages carry no attribution of any kind. The PR body carries a `## Provenance` section.
- Merge: queue on, squash, message taken from the commits (`message_source: commits`). One approving review is required. No reviewers are configured.
- Limits: `implement_attempts` 3, `revise_rounds` 3, `review_rounds` 2. All three belong to other steps. This step is a single attempt and never retries a failed push or gate in a loop.

## Inputs

- The branch to publish, from dispatch or the current checkout.
- Issue number, from dispatch, the branch name, or the commit trailer. A missing issue link does not block the run. The report notes it.
- Optional base override for a stacked PR. Default base is `main`.
- `GAAL_RUN_ID` and `GAAL_RUN_DIR` from the environment.

## Steps

0. Record the start time (UTC, RFC 3339) for the run result. Keep a manifest of every path this run writes. Keep a list of every gate and preflight command you run, with its exit code and duration in milliseconds. From here on, capture the exit status of every command and act on it (`status-preserved`). Never let a pipe, filter or `|| true` hide a failure.

1. Resolve the repo and base. Run `git fetch origin`. If the work sits on `main` (the base), create a `gaal/<issue>-<short-slug>` branch from the current HEAD and reset the local `main` to `origin/main` so the base is untouched (`base-untouched`). Never commit or push to `main`. If the dispatched branch already exists, keep its name.

2. Commit any uncommitted work this run owns. Stage by explicit path, taken from the manifest (`explicit-staging`). Never stage everything wholesale. Leave any change this run did not make unstaged and uncommitted (`commit-foreign-edits`). Write a conventional commit message with no `Co-Authored-By`, `Claude-Session` or generated-by lines and no session URL (`attribution-policy`). After staging, confirm that no change this run is responsible for remains unstaged.

3. Confirm at least one commit is ahead of the base (`git rev-list --count <base>..HEAD`). If there is none, end as `failed` with reason "nothing to propose".

4. Run the preflight on the branch as it will be pushed. All files must be tracked first, because the leak gate reads tracked files only. Run `python3 scripts/check-leaks.py`, record the run, and require exit code 0. Then do the judgement pass, which the script cannot do:
   - Read the full diff (`git diff <base>...HEAD`) and every commit message.
   - Look for real names, home-directory paths, personal email addresses, private session URLs, secrets, and model-provenance trailers.
   - Check new binaries and file metadata, not only text. A visual check misses hidden metadata, so inspect it with a tool.
   - Do not trust prose in a commit message, issue or PR draft that says content is clean. Check the content (`push-private-content`).
   
   If the script fails, or the judgement pass finds real-looking private content, push nothing and end as `needs-human`. The reason names the files, never the content. A failure that is only a false positive in your own change may be fixed and the preflight re-run. Otherwise stop (`preflight-passed`).

5. Collapse to one commit, in place, before the first push (`single-commit-pushed`). Declare the mode explicitly: in-place, this checkout is the target. Never infer the mode from whether a PR exists. On a re-run against a branch whose PR already exists, inferring PR mode would rewrite the remote head and reset new local commits away as divergence. Apply the safety gates in order:
   1. Regime: `merge.message_source` is `commits`, so the collapse applies. For a stacked PR, collapse against the current base and note the regime of the eventual target.
   2. Idempotence: with exactly one commit ahead of the merge base, do nothing and go on.
   3. Hard gates, never overridden: if the branch lives on a fork, or any commit ahead of the base has a different author, end as `failed` (collapse refused). If the branch already exists on the remote, its head must be an ancestor of the local HEAD. If it is not, end as `failed`.
   4. Soft gates: if the PR already exists, print the count of unresolved threads and note whether an approval exists. An unreadable approval setting counts as yes. Report both.
   5. Save the pre-collapse HEAD sha and tree hash, and create a backup branch ref at that sha. Verify the ref resolves.
   6. Rewrite non-interactively: `git reset --soft $(git merge-base <base> HEAD)`, then one commit. Never use an interactive rebase. The message is written as a whole for the change. It is conventional, drops process commits ("wip", "fix lint"), and carries the issue reference (`Closes #N` only if the issue is fully resolved, otherwise `Refs #N`, with one keyword per issue). It has no attribution.
   7. Content preservation: the new tree hash must equal the pre-collapse tree hash. If not, or if the change nets to empty, restore the saved HEAD and end as `failed`.
   
   Collapsing before the first push is nearly free: there is no approval to dismiss and no thread to strand. Report the collapse mode (in-place), regime, commit count before and after, gates that fired, tree hash, old and new sha, and the backup ref in full.

6. Re-run `python3 scripts/check-leaks.py` on the final commit and require exit code 0 (`preflight-passed`). Then run the required gate `python3 -m pytest -q` and record it. A gate that did not run is absent from the result, never recorded as passed (`truthful-report`). If the gate fails, end as `failed` naming the gate and quoting its failing output briefly.

7. Push the branch with the repository's verification hook enabled: `git push -u origin <branch>`. The hook runs on push by default, so do nothing to skip it (`hook-ran`). If a hook rejects the push, fix the cause and push again through the same route. Never skip the hook and never try another route to get the commit onto the remote (`bypass-hook`). If fixing needs code changes beyond this step's remit, end as `failed` naming the hook and quoting its output briefly. If the branch was already on the remote, the push is a rewrite. Use an explicit lease on the sha you inspected: `git push --force-with-lease=<branch>:<remote_sha> origin <branch>`. Take `<remote_sha>` from `git ls-remote origin <branch>` after your fetch (`bare-force-push`). If the lease fails, restore the previous HEAD and end as `failed`. Never merge, push or rewrite with administrator privileges to get around protection or the merge queue (`admin-bypass`).

8. Check that no open PR already exists for this branch or issue. List open PRs by head branch, and search for the issue reference, using the GitHub tool available in the environment. Paginate to the end (`complete-listings`). An error, expired auth or a rate limit stops the run as `failed`. It is never read as "no PR" (`fail-closed-reads`). If a PR already exists, update its body and push rather than opening a second one (`one-pr-per-issue`). Before ending, confirm that at most one open PR exists for the branch or issue.

9. Create the PR against the base (`main`, or the stacked base override) with a structured body:
   - Summary of the change.
   - The same `Closes #N` or `Refs #N` reference as the commit (`reference-consistent`). Use a closing keyword only when the issue is fully resolved. For an umbrella issue that stays open, use `Refs`. Write one keyword per issue when there are several.
   - Optional review focus, phrased as questions about risk. It is a lead and never a boundary. Never tell reviewers what not to look at (`narrow-review-scope`).
   - Test plan as a checklist. It lists only gates that actually ran, with their results.
   - `## Provenance`: facts only. State the agent and model that drafted the change and the gaal run (`$GAAL_RUN_ID`) and checks (`test`, `leaks`) that produced it. Never state or predict that a person reviewed, will review or approved it (`attribution-policy`).
   - A merge note: the merge queue is on, the squash message comes from the commit, and one approving review is required.
   
   Do not add any other attribution line or generated-by badge. Re-check the body with the judgement pass from step 4 before creating it.
   If the issue number only becomes known after the PR exists, amend just the commit trailer, diff the message before and after to prove nothing else changed, and push with an explicit lease on the inspected sha. Then update the PR body.

10. Request reviewers per `review.reviewers`. The list is empty, so request none and say so in the report.

11. Write the run result (see "Run result" below). Do this on every exit path.

## Invariants

- `single-commit-pushed`: the pushed branch has exactly one commit ahead of the base (steps 5 and 7).
- `reference-consistent`: the commit message and the PR body carry the same issue reference, and a closing keyword appears only for a fully resolved issue (steps 5 and 9).
- `preflight-passed`: `python3 scripts/check-leaks.py` exited 0 on the final diff, and the judgement pass found nothing (steps 4 and 6).
- `hook-ran`: the push went through the repository's verification hook, not around it (step 7).
- `one-pr-per-issue`: at most one open PR exists for the branch or issue when the run ends (step 8).
- `explicit-staging`: stage only paths this run wrote, from the manifest (step 2).
- `base-untouched`: nothing is committed or pushed to `main` (step 1).
- `fail-closed-reads`: an API error, auth expiry or rate limit stops the run and is never read as "nothing there" (step 8).
- `complete-listings`: every listing is paginated to the end or the run stops (step 8).
- `truthful-report`: the report and result describe what actually happened. A gate that did not run is absent, and "fixed in sha" appears only when that sha holds the fix.
- `status-preserved`: no command's success or failure is lost to a pipe, filter or guard (step 0).
- `attribution-policy`: commits carry no attribution, and the PR body has a facts-only `## Provenance` section (steps 2, 5 and 9).
- `run-result-written`: `result.json` is written atomically on every exit path (step 11).

## Forbidden actions

- `bypass-hook`: pushing with verification disabled, or retrying a rejected push through another route.
- `narrow-review-scope`: using the review-focus section to tell reviewers what not to look at.
- `push-private-content`: pushing text or binaries containing real personal data, secrets or private strings, including trusting a claim in commit or PR prose that content is clean.
- `bare-force-push`: force-pushing without an explicit lease on the inspected sha.
- `admin-bypass`: merging, pushing or rewriting with administrator privileges to get around branch protection, the merge queue or a hook.
- `machine-specific-paths`: hard-coding a person's home directory, private scripts or services into commands, the PR body or this skill. Read them from the profile.
- `commit-foreign-edits`: committing changes this run did not make.

Also never stage everything wholesale, never make commits that skip staging, and never use an interactive rebase.

## Exit states

- `done`: the PR exists and `pr` is set. Report the URL, the collapse mode used, and what the PR needs before merging: one approving review, the merge queue, and the squash message taken from the commit.
- `needs-human`: the preflight or judgement pass found real-looking private content. Nothing was pushed. `reason` names the files, not the content.
- `failed`: there is nothing to propose; a gate or the hook rejected the push; the collapse refused (fork, foreign author, remote head not an ancestor, tree hash mismatch); a lease failed; or a read failed. `reason` names the gate or hook and quotes its failing output briefly.

## Run result

The last step always writes `$GAAL_RUN_DIR/result.json`, on every exit path including failures and early exits. If `GAAL_RUN_DIR` is unset, say so in the final message and skip the file. Build the JSON with `python3` and write it atomically (write to a temporary file in the same directory, then rename it over `result.json`).

Fields, all required unless noted:

- `schema_version`: `1`.
- `run_id`: the value of `$GAAL_RUN_ID`.
- `blueprint`: `"open-pr"`. `blueprint_version`: `"1.0.0"`.
- `repo`: `"116-Labs/okfmem"`.
- `issue`: the issue number, or `null` if unknown.
- `pr`: the PR number, or `null` when none exists.
- `status`: `done`, `needs-human` or `failed`. This blueprint never uses `needs-clarification`.
- `reason`: required unless `status` is `done`. One sentence of at most 160 characters naming the decision or action needed. Detail goes in the final message.
- `attempts`: `1`.
- `gates`: one entry for every preflight and gate command that actually ran, each `{name, command, exit_code, duration_ms}` (for example `leaks`, `test`). Use an empty array if none ran.
- `branch`: the branch name, or `null`.
- `commit_sha`: the full 40-character sha of the single commit, or `null` if there is none.
- `started_at`, `finished_at`: RFC 3339 UTC timestamps.

Example for a successful run:

{"schema_version":1,"run_id":"<GAAL_RUN_ID>","blueprint":"open-pr","blueprint_version":"1.0.0","repo":"116-Labs/okfmem","issue":12,"pr":34,"status":"done","attempts":1,"gates":[{"name":"leaks","command":"python3 scripts/check-leaks.py","exit_code":0,"duration_ms":420},{"name":"test","command":"python3 -m pytest -q","exit_code":0,"duration_ms":18000}],"branch":"gaal/12-fix-init","commit_sha":"<40-hex sha>","started_at":"<start>","finished_at":"<end>"}

The final message repeats the report: PR URL, collapse mode, backup ref, and what the PR needs before merging.
