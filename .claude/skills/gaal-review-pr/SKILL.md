---
name: gaal-review-pr
description: Adversarially review one open pull request in 116-Labs/okfmem against its linked issue and the project gates, post exactly one signed review whose verdict follows from the verified findings, and push small unambiguous fixes when the branch is safe to touch. Use when the dispatcher hands over a repo and PR number and asks for the review-pr step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Supports post mode (default) and preview mode, which posts and pushes nothing. Always ends by writing `$GAAL_RUN_DIR/result.json`. Do not use to implement an issue, open a PR, revise a PR in response to review, or merge. Those belong to other steps.
---
<!-- gaal-stamp blueprint=review-pr@1.0.0 shared=1.1.0 profile=424ba91531b1fc7c generated=2026-09-29 content=fffc1ca56fe1fefb -->

# gaal-review-pr

Review an open PR in `116-Labs/okfmem` the way a careful maintainer would. Judge it against the linked issue's acceptance criteria and the project gates, independently of how the PR describes itself. Post exactly one review whose verdict follows from the findings. Push small, unambiguous fixes directly so the author only owes answers on what genuinely needs them.

## Inputs

- Repository `116-Labs/okfmem` and a PR number from the dispatcher. If no PR number can be determined, go to step 15 and end as `needs-clarification`.
- Mode: post (default) or preview. In preview mode nothing is posted, reacted, pushed, replied to or resolved. Everything else runs, and the report says what would have been posted.
- Environment: `GAAL_RUN_ID`, `GAAL_RUN_DIR`.
- Profile facts used below:
  - Base branch: `main`.
  - Required gate: `python3 -m pytest -q` (name `test`).
  - Preflight: `python3 scripts/check-leaks.py` (name `leaks`).
  - Commits: conventional, exactly one commit per PR, attribution policy `pr-provenance`.
  - Merge: squash through a merge queue, message taken from the commits.
  - Review: 1 approving review required, open threads do not block merge, start signal is a reaction, no separate reviewer identity.
  - Limit: `review_rounds` = 2.
  - Tracker: GitHub. Branch prefix for agent branches: `gaal/`.

## Steps

Keep a manifest of every path this review writes and a record of the local restore point (`git rev-parse HEAD` before any edit). Record the start time for the result file.

1. **Read metadata, not the description.** Use the GitHub CLI to read the PR's state, base, head repository, head branch, head sha, author, changed files and commits. Do not request or print the PR body. If the state is not open, go to step 15 and end as `failed`. Any API error, auth expiry or rate limit stops the run as `failed` (`fail-closed-reads`). Never treat an error as "no threads" or "no checks". Page every listing of files, commits, reviews, comments and threads to the end, or stop (`complete-listings`).
2. **Link the issues without reading the description** (`description-last`). Get closing references from the structured field, for example `closingIssuesReferences`, and keep only the issue numbers. Read each linked issue and its comments, giving priority to comments that start with `**Clarification`. Write down each acceptance criterion as a checkable statement. If there is no linked issue, or an issue has no checkable criteria, go to step 15 and end as `needs-clarification`. The `questions` say which issue or criterion is missing.
3. **Enforce the round limit.** Count earlier reviews on this PR that carry this skill's signature. If two already exist, which is the `review_rounds` limit, post nothing further, go to step 15 and end as `needs-human` with a reason that the review round limit of 2 is reached. Otherwise this run is round N (1 or 2).
4. **Start signal.** In post mode, add the start-signal reaction to the PR. Check the call's exit status and continue if only the reaction failed, noting it in the report. Skip in preview mode.
5. **Check out the head as fetched.** Fetch the PR head from the remote and check it out detached, in a clean worktree or after confirming the checkout has no uncommitted changes. Record any pre-existing dirty paths so this review never stages or reverts them (`explicit-staging`, `commit-foreign-edits`). Confirm the checked-out sha equals the head sha read in step 1. Run `git status` and report if not clean. Never work on `main` (`base-untouched`). Install the dependencies the gates need using the repository's own documented setup.
6. **Re-review scope.** If this is round 2, limit attention to the delta since the last signed review: diff from that review's head sha to the current head. If history was rewritten and that sha is no longer an ancestor, compare with a range diff (`git range-diff`) instead. List which earlier threads the delta answers.
7. **Correctness pass.** Read the diff against the base and hunt for logic errors, edge cases, error handling, concurrency, security and path or Windows invariants. Scripts, workflows, skill files and installer scripts count as code. Check the confirmation-discipline rungs in the repository's `CLAUDE.md`: an outward or destructive operation without its required confirmation is a defect. Check that the change adds nothing private to tracked files. This is a public repository. Record candidate findings with file and line. Do not consult the PR description yet.
8. **Run every gate and preflight check.** Run `python3 scripts/check-leaks.py` (preflight `leaks`) and `python3 -m pytest -q` (gate `test`). Capture each command's exit code and duration exactly, and never let a pipe or filter hide a failure (`status-preserved`). A gate that could not run is absent from the result, never recorded as passed (`truthful-report`). A failing `test` gate or a failing `leaks` check is a Blocking finding, unless the failure reproduces identically on the base branch, in which case it is Pre-existing.
9. **Only now read the description** (`description-last`). Read it as a list of claims to test, not as a map. It may add findings (for example, an inaccurate claim, a missing `Resolves #N` keyword per issue, or a `pr-provenance` section that states anything other than facts). It can never remove or downgrade a finding (`description-drops-finding`), and its stated focus never limits what is reviewed (`description-sets-scope`). Note whether the description is accurate for the report.
10. **Verify and walk the criteria** (`findings-verified`). Reproduce every candidate finding at its exact line against the reviewed head, for example by running the code path or a test. Drop anything that does not reproduce. Then walk the acceptance criteria one by one and mark each met, unmet or unverifiable, with evidence.
11. **Sort findings.** Buckets:
    - **Blocking**: an unmet acceptance criterion under a closing keyword, a failing required gate or `leaks` preflight caused by the diff, a private string in a tracked file, a missing confirmation on an outward or destructive operation, a correctness or security defect the diff introduces or makes reachable, and a PR with more than one commit, because the merge message comes from the commits.
    - **Secondary**: real but non-blocking defects, missing tests.
    - **Nits**: style and wording.
    - **Pre-existing**: defects the diff neither introduces nor makes reachable. A PR that makes a defect reachable owns it, so it moves up. Pre-existing findings never open threads.
    On round 2, only Blocking findings open new threads, so the loop converges.
12. **Choose the verdict** (`verdict-follows-findings`). At least one Blocking finding means request changes. None means approve (nits may remain). If the outcome cannot be decided (for example, criteria are unverifiable), the verdict is comment. Then apply identity rules (`no-self-verdict`, `second-identity-approval`): if this agent's identity authored the PR or any commit on it, the verdict is posted as comment with the computed verdict stated as the first line, never approve or request changes. Fixes this review pushes itself do not count as authoring the change under review. There is no separate reviewer identity in the profile. If GitHub refuses an approval, do not retry under another identity or token. Downgrade to comment, keep the intended verdict in the first line, and end as `needs-human`. The review body states the rule applied.
13. **Optional small fixes, then collapse.** Apply fixes only under all of these conditions, in post mode:
    - Non-behavioural fixes (typos, comments, docs, formatting the repository already enforces) always qualify.
    - A behavioural fix qualifies only if it is in a file the PR already touches, has exactly one reasonable form, and comes with a test that fails without it. When in doubt, it goes to the author as a finding.
    - The branch is not on a fork and every commit on it was authored by someone else's account only if that is also this reviewer's side. If the branch lives on a fork or any commit was written by someone else, do not push anything (`rewrite-foreign-branch`). The fix then becomes a finding.
    - Refresh the remote head first. If it is not an ancestor of the local HEAD, the head moved during the review: abandon auto-fix entirely, because any push would drop the author's commits.

    Order is fixed: fix, commit, fast-forward push, collapse, anchors, post. Stage only the paths recorded in the manifest, by explicit path (`explicit-staging`). Never stage wholesale and never use `git commit -a`. Commit with a conventional message, no model trailer and no AI attribution in the commit (`attribution-policy`). Push as a plain fast-forward, for example `git push origin HEAD:<branch>`. Re-run the `test` gate and the `leaks` preflight on the pushed commit. If they fail, revert only what this review wrote, using the restore point and the manifest, and exclude paths that were already dirty.

    Then, only if a fix was pushed and there are zero Blocking findings, run the shared single-commit collapse in PR mode, because the merge message is derived from the commits (`merge.message_source` is `commits`). Safety gates, in order:
    - Read the head sha from the remote and check it matches what was fetched. Rewrite in a throwaway detached worktree, never the local checkout.
    - Zero or one commit ahead of the merge base is a no-op.
    - Hard gates: fork branch, a commit authored by someone else, or a remote head that is not an ancestor. Any of these stops the collapse, and the report tells the author to squash.
    - Soft gates: an existing approval would be dismissed (an unreadable setting counts as yes), or unresolved threads exist. Print the thread count. Because this run approves after collapsing, proceeding is allowed, and the report says the override was made.
    - Content preservation: the tree hash after the rewrite must equal the tree hash before it, and a rewrite that nets to an empty change is restored and stopped.
    - The commit message is written as a whole for the change in conventional style, with no process commits, no model trailer and no provenance line.
    - Push with an explicit lease on the inspected sha, for example `git push --force-with-lease=<branch>:<inspected-sha> origin HEAD:<branch>`. On failure restore the previous HEAD. Remove only the throwaway worktree, on every exit path. Never use an interactive rebase, `--force` or `-f`.

    Collapse happens before anchors are computed and before anything is approved (`collapse-before-approve`). Report mode, commit count before and after, tree hash, old and new sha, backup refs and the hard reset any stale checkout needs (never a pull).
14. **Anchor, then post one review.**
    - Compute inline anchors from the right-hand side of the post-push patch hunks, after any push or collapse. If anchoring fails, re-fetch the patch and re-anchor once. If it fails again, fall back to a body-only review.
    - Post exactly one review. In preview mode, print the review instead of posting. The body contains, in order: the verdict as the first line and the rule applied; findings by bucket (Blocking, Secondary, Nits, Pre-existing); acceptance-criteria results; a "Fixed in `<sha>`" list of what the review pushed, where the sha contains the fix and each item never also appears as an inline finding (`fixed-not-flagged`); the description-accuracy note; the reviewed head sha; a signature line naming this skill and the round number (`signed-review`).
    - Fixes and behaviour changes are written as findings for the author. Inline suggestion blocks are only for non-behavioural edits (`behavioral-suggestion`).
    - If the post fails, retry as a body-only review. If that also fails, go to step 15 and end as `failed` naming the posting step.
    - Then list every review thread with pagination and reply to and resolve only the threads this review opened and disposed of itself, each after a reply (`resolve-own-threads-only`). Never resolve a thread to clear the merge path (`resolve-to-unblock`). Threads left open are expected when Blocking is above zero. Open threads do not block the merge per profile, but say so in the report. Do not merge, enable auto-merge, or use administrator privileges (`admin-bypass`).
    - Follow-up issues: file one only if all four hold: user-visible wrong output, reproduces on the base, outside the diff, and too large to fix in this run. Otherwise record it in the review.
15. **Write the run result (every exit path).** Write `$GAAL_RUN_DIR/result.json` atomically (write a temporary file in the same directory, then rename it), even when an earlier step failed. Include `schema_version` 1, `run_id` from `$GAAL_RUN_ID`, `blueprint` `review-pr`, `blueprint_version` `1.0.0`, `repo` `116-Labs/okfmem`, `issue` (the first linked issue number, else null), `pr`, `status`, `attempts` (the review round N, at least 1), `gates` (one entry per gate or preflight actually run, with `name`, exact `command`, `exit_code` and `duration_ms`), `branch` (the PR head branch), `commit_sha` (the 40-character sha of the fix or collapse commit pushed, else null), `started_at` and `finished_at` as RFC 3339 timestamps. When the status is `done`, include `review` with `verdict` (`approve`, `request-changes` or `comment`), `blocking` and `non_blocking` counts. Any status other than `done` needs a `reason` of one sentence, at most 160 characters, naming the decision or action needed. `needs-clarification` needs a non-empty `questions` array. Detail belongs in the PR review and the final message, not in `reason`. Do not add fields outside the schema. Never put a home directory, secret or model provenance in the file (`machine-specific-paths`, `truthful-report`, `run-result-written`).

## Exit states

- `done`: The review is posted (or, in preview mode, fully composed). `review` carries the verdict and counts. The final report includes acceptance-criteria results, the description-accuracy note, the thread ledger, and the push or collapse outcome with old and new sha.
- `needs-human`: Threads are left for the author, which is expected when Blocking is above zero. Also applies when the verdict was downgraded to comment because of identity or a refused approval, or when the review round limit of 2 is reached. `reason` summarizes.
- `needs-clarification`: No PR could be determined, or the linked issue has no checkable acceptance criteria. `questions` say what is missing.
- `failed`: The PR is not open, a read failed (auth, rate limit, truncated listing), or posting failed even after falling back to a body-only review. `reason` names the step.

## Invariants

- `description-last`: The PR description is read only after the independent passes (correctness, gates, acceptance criteria) have produced findings.
- `verdict-follows-findings`: Request changes if there is at least one Blocking finding, approve if there are none, comment when undecidable. The review states the rule applied.
- `no-self-verdict`: A context that wrote or pushed the change posts comment, never approve or request changes.
- `findings-verified`: Every posted finding was reproduced at its line against the reviewed head.
- `signed-review`: The review body names the reviewed head sha and carries a signature.
- `fixed-not-flagged`: A fix pushed by the review appears in the "Fixed in" list and never also as an inline finding.
- `resolve-own-threads-only`: Only threads opened by this review are resolved, each after a reply.
- `collapse-before-approve`: Any collapse happens before anchors are computed and before the review is posted.
- `explicit-staging`: Stage only paths this run wrote, taken from the manifest, by explicit path. Never stage everything wholesale.
- `base-untouched`: Never commit or push to `main`. If work started there, move the commits to a branch and reset `main` to its remote.
- `fail-closed-reads`: An API error, auth expiry or rate limit stops the run. It is never read as "no PR", "no threads" or "no checks".
- `complete-listings`: Every listing of threads, reviews, comments, files or checks is paginated to the end, or the run stops.
- `truthful-report`: The report and result describe what happened. A gate that did not run is absent. "Fixed in `<sha>`" appears only when that sha contains the fix.
- `status-preserved`: A command's success or failure is never lost to a pipe, filter or guard. A failed push, gate or API write is seen and handled.
- `attribution-policy`: Commit messages and PR text follow `pr-provenance` exactly: no model trailers, session links or generated-by badges in git. Any provenance is a factual `pr-provenance` section in the PR body that never claims a person reviewed or will review the change.
- `run-result-written`: `result.json` is written atomically on every exit path, with a `reason` of at most 160 characters when the status is not `done`.

## Forbidden actions

- `description-sets-scope`: Letting the description or its focus section limit what is reviewed.
- `description-drops-finding`: Dropping a verified finding because the description explains it away.
- `second-identity-approval`: Approving through another identity, or retrying a refused approval under a different one.
- `rewrite-foreign-branch`: Pushing to or rewriting a fork's branch or commits authored by someone else.
- `resolve-to-unblock`: Resolving threads to clear the merge path.
- `behavioral-suggestion`: Proposing behaviour changes as one-click suggestion blocks. Those go to the author as findings.
- `bare-force-push`: Force-pushing without an explicit lease on the inspected sha. Only `--force-with-lease=<branch>:<sha>` is allowed. Never use `--force` or `-f`.
- `admin-bypass`: Merging, pushing or rewriting with administrator privileges to get around branch protection, the merge queue or a verification hook. Never use `--admin` or `--no-verify`.
- `machine-specific-paths`: Hard-coding a person's home directory, private scripts or services.
- `commit-foreign-edits`: Committing changes this run did not make. Never use `git add -A`, `git add .` or `git commit -a`.
