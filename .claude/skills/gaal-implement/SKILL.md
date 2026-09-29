---
name: gaal-implement
description: Implement one qualified GitHub issue in 116-Labs/okfmem as a verified change on its own `gaal/` branch holding exactly one commit. Use when the dispatcher hands over a repo and issue number that passed qualification and asks for the implement step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Ends by writing `$GAAL_RUN_DIR/result.json`. Do not use to open the PR, respond to review, review a PR, merge, or deploy. The PR step belongs to `open-pr`.
---
<!-- gaal-stamp blueprint=implement@1.0.0 shared=1.0.0 profile=424ba91531b1fc7c generated=2026-09-29 content=d5fdb4b19608ff6b -->

# gaal-implement

Turn one qualified issue into a branch off `main` holding **exactly one commit** that meets the issue's acceptance criteria and passes every gate. The run is headless, so never wait on input (`ask-mid-run`). Anything you cannot resolve ends the run as `needs-clarification`, `needs-human` or `failed`. Every exit path, including failures, ends with Step 13.

Repo: `116-Labs/okfmem`. Base: `main`. Tracker: GitHub. Branch prefix: `gaal/`. Commit convention: conventional. Attribution policy: `pr-provenance`. Merge: squash through a merge queue, with the message taken from the commits. Gate: `python3 -m pytest -q` (required). Preflight: `python3 scripts/check-leaks.py`. Attempt limit: 3 (`limits.implement_attempts`).

## Inputs

- Issue number, from dispatch. The repo is `116-Labs/okfmem`.
- Base ref: `main`, or a parent branch if dispatch says the work is stacked. Use a stacked-branch tool only if one is present; otherwise fall back to a single branch off the base.
- `GAAL_RUN_ID` and `GAAL_RUN_DIR` from the environment.

At the very start, record `started_at` as an RFC 3339 UTC timestamp. Keep three things for the whole run: a **manifest** (every path you write), an **attempt counter** (starts at 0), and a **gate log** (name, exact command, exit code, duration in ms for every gate or preflight you run).

## Steps

1. **Resolve the repo and the issue.** Confirm the checkout's remote is `116-Labs/okfmem` and fetch the issue from GitHub. If either cannot be resolved, stop, never guess. A tool, auth or network failure is `failed`. Read failures stop the run and are never treated as "no comments" (`fail-closed-reads`).

2. **Read the issue body and every comment.** Page through the comment listing to the end. If you cannot, stop (`complete-listings`). Comments that clarify or narrow scope are part of the spec. When a comment says the body was rewritten, the current body wins. Extract the acceptance criteria as a numbered list.

3. **Check the issue's references against the current code.** Open every file, symbol and line it names. If they have drifted beyond what a plan can safely assume, note the drift. It will feed Step 5.

4. **Look for existing code that owns the capability.** Search the repo (the `memory_*.py` modules and the `okfmem` dispatcher, `scripts/`, `plugins/`, tests) for helpers already doing the work. Extend them rather than building a parallel one. The core is standard-library-only Python 3.11+, so add no dependency. If building new is justified, record why in the plan.

5. **Write the plan before any source edit** (`plan-before-code`). List the files to change, the tests to add, and, for each acceptance criterion, how it will be shown to hold. Every criterion must map to something in the plan. If the issue is ambiguous, or the drift from Step 3 is too large to plan around, end as `needs-clarification` with specific, answerable questions that each name the file or criterion they concern.

6. **Apply the guards.** If the plan needs a breaking change to a public contract, a store or page format, or the CLI surface that the issue does not explicitly ask for, do not make it (`unapproved-breaking-change`). End as `needs-human` and say what a human must decide. If the plan needs a deploy, publish or migration against a shared environment, do not run it (`deploy`). Put the commands in the report for a human.

7. **Check the checkout and look for resumable work.**
   - Run `git status --porcelain` before touching anything. Record every pre-existing change. Never commit, revert or reformat them (`absorb-stray-edits`, `commit-foreign-edits`). If you cannot tell whose they are or whether they overlap the plan, end as `needs-human`.
   - Untracked `.gaal/` is normal engine state. Leave it alone.
   - Look for an existing `gaal/` branch or worktree for this issue. Resume it only if its state is sound: authored by this automation, based on the current base, and with no foreign commits or edits. Otherwise start fresh.

8. **Create the branch.** Branch from the base as `gaal/<issue-number>-<short-slug>`. Never commit or push to `main` (`base-untouched`). If work has somehow started on the base, move it to the feature branch and reset the base to its remote. Use a separate worktree if dispatch asks for one. The core needs no dependency install, but run whatever the repo's own docs require for pytest if it is missing. If that install fails, end as `failed`.

9. **Implement the plan.** Read each file before changing it. Keep edits minimal. Add the tests from the plan. Add every path you write to the manifest, including new files. A path missing from the manifest would slide into another issue's commit later, so update the manifest as you go, not at the end. Do not fix pre-existing debt outside the change's scope (`unrelated-refactor`). Note it for the report. Follow the repo's hard rules: no real home paths, no private session URLs, no personal email, no model provenance in any tracked file.

10. **Run preflight and gates, and count attempts** (`gates-green`, `bounded-attempts`). Each pass runs, in order:
    - `python3 scripts/check-leaks.py`, the preflight leak check. It must exit 0.
    - `python3 -m pytest -q`, the required `test` gate. It must exit 0.

    Log the exact command, exit code and duration of each one. If either fails, increment the attempt counter, fix the cause, and run both again. The last full pass must run on the final tree. Any edit after a green pass means another full pass. When the counter reaches **3** and a run is still red, stop and end as `needs-human` with the failing output summarized. Never loop past 3. Never skip a hook or check (no `--no-verify`). Report exactly which gates ran, which did not, and why (`truthful-report`).

11. **Stage exactly the manifest and commit once** (`explicit-staging`, `manifest-matches-staged`, `single-commit`, `issue-trailer`).
    - Stage by explicit path from the manifest, one `git add -- <path>` per path or a single command listing them. Never use `git add -A`, `git add .` or `git commit -a`.
    - Check `git diff --cached --name-only` equals the manifest, and that `git status --porcelain` shows nothing else this run is responsible for. If they differ, fix the manifest or the index before committing.
    - Commit message: conventional format (`type(scope): summary`), written as a whole for the change and not as process notes. Describe what changed and why. In the body, reference the issue: `Closes #<n>` only if **every** acceptance criterion is met, otherwise a plain `Refs #<n>`.
    - Attribution follows `pr-provenance` (`attribution-policy`): **no** `Co-Authored-By:` trailer naming a model, no `Claude-Session:` trailer, no session URL, no "Generated with" badge in the commit. Provenance goes only in the PR body, which `open-pr` writes. Add nothing on your own initiative.
    - Check the commit with `git status` and that the exit code of `git commit` was 0 (`status-preserved`). Do not pipe it away.

12. **Collapse to one commit if needed.** The merge is a squash with `message_source: commits`, so the collapse applies. Run `git rev-list --count <base>..HEAD`.
    - 0 is a mistake: nothing was committed. Go back to Step 11.
    - 1 is fine, do nothing (idempotent).
    - More than 1: collapse **in place**, in this checkout, no PR mode. Never use interactive rebase. Record the tree hash with `git rev-parse HEAD^{tree}`. Run `git reset --soft $(git merge-base <base> HEAD)` and make one commit with a whole-change message per Step 11. If the result is an empty change, restore the previous HEAD and stop. After committing, confirm the new tree hash equals the recorded one. If not, restore the previous HEAD and end as `needs-human`. If any commit on the branch is authored by someone else, the collapse is a hard-gate stop: end as `needs-human`. Do not push. A stacked rebase conflict is also `needs-human`.
    - Confirm `git rev-list --count <base>..HEAD` is exactly 1. If you changed the tree after the last green pass, go back to Step 10.

13. **Write the run result on every exit path** (`run-result-written`). Write `$GAAL_RUN_DIR/result.json` atomically: write a temp file in `$GAAL_RUN_DIR`, then rename it over `result.json`. If `GAAL_RUN_DIR` is unset, report that in your final output. It must be valid JSON matching the run-result schema:
    - Required fields: `schema_version` (the number `1`), `run_id` (from `$GAAL_RUN_ID`), `blueprint` (`"implement"`), `blueprint_version` (`"1.0.0"`), `repo` (`"116-Labs/okfmem"`), `issue` (the integer, or `null` only if it could not be resolved), `pr` (`null`), `status`, `attempts`, `gates`, `branch`, `commit_sha`, `started_at`, `finished_at` (RFC 3339 with a timezone, e.g. `2026-09-29T12:00:00Z`).
    - `attempts` is an integer of at least 1 and counts gate-fix cycles consumed. Use 1 if the run ended before any gate ran.
    - `gates` lists only what actually ran, each as `{name, command, exit_code, duration_ms}`, with the exact command. Include the preflight leak check as `leaks`. A gate that did not run is absent, never listed as passed. Use `[]` if none ran.
    - `branch` is the branch name, or `null` if none was created. `commit_sha` is the 40-character lowercase sha of the single commit, or `null` if none exists. "Fixed in `<sha>`" may appear only if that sha contains the fix.
    - Conditional rules: `reason` is required, non-empty, unless status is `done`. `questions` is required and non-empty when status is `needs-clarification`. No other fields are allowed.
    - Also print a short report: each acceptance criterion as met or not met, gates run, pre-existing debt noticed, and any commands a human must run.

## Exit states

- `done`: The single commit exists (`git rev-list --count <base>..HEAD` is 1) and the preflight and required gate exited 0 on the final tree. `branch`, `commit_sha` and `gates` are set. A partial result uses `Refs #<n>`, never a closing keyword, and the report says which criteria are unmet.
- `needs-clarification`: The spec is ambiguous, or the code has drifted from what the issue describes. `reason` summarizes it and `questions` are specific, answerable, and each name a file or criterion. No commit is made.
- `needs-human`: The 3-attempt limit was reached; a guard fired (breaking change, deploy, migration); the checkout has edits of unclear ownership; a collapse safety gate failed; or a stacked rebase conflicted. `reason` says which and what a human should do.
- `failed`: Environment or tooling problems: repo or tracker unreachable, auth or rate limit, broken dependency install, disk. `reason` includes enough detail to retry.

## Invariants

- `plan-before-code`: A written plan exists before the first source edit, and every acceptance criterion maps to it (Step 5).
- `single-commit`: At hand-off the branch has exactly one commit ahead of the base (Steps 11–12).
- `manifest-matches-staged`: The commit's files are exactly the manifest of paths this run wrote (Step 11).
- `gates-green`: Every required gate, plus the preflight, ran on the final tree with exit 0 and is listed in the result (Step 10).
- `issue-trailer`: The commit references the issue, with a closing keyword only if every criterion is met (Step 11).
- `bounded-attempts`: Gate-fix cycles stop at 3, then `needs-human` (Step 10).
- `explicit-staging`: Stage only manifest paths, by explicit path (Step 11).
- `base-untouched`: Never commit or push to `main` (Step 8).
- `fail-closed-reads`: Errors, auth expiry and rate limits stop the run and never become "no comments" (Step 1).
- `complete-listings`: Comment listings are read to the end or the run stops (Step 2).
- `truthful-report`: Report and result state only what happened (Steps 10, 13).
- `status-preserved`: Check the exit status of every gate, commit and write. Never lose one to a pipe or filter (Steps 10, 11).
- `attribution-policy`: Follow `pr-provenance` exactly. No model provenance in the commit (Step 11).
- `run-result-written`: `result.json` is written atomically on every exit path (Step 13).

## Forbidden actions

- `absorb-stray-edits`: Do not commit, revert or reformat changes that were in the checkout before the run.
- `deploy`: Do not run deploys, migrations against shared environments, or publishes. Return the commands for a human.
- `unapproved-breaking-change`: Do not break a public contract, schema or persisted format unless the issue explicitly asks.
- `unrelated-refactor`: Do not fix pre-existing debt outside the change's scope. Record it in the report.
- `ask-mid-run`: Do not wait on interactive input. End as `needs-clarification` with questions.
- `bare-force-push`: Do not force-push. This step pushes nothing. If a lease were ever needed it would be `--force-with-lease=<branch>:<sha>` on the inspected sha, and never `--force` or `-f`.
- `admin-bypass`: Do not merge, push or rewrite with admin privileges to get around branch protection, the merge queue or a verification hook. Do not use `--admin` or `--no-verify`.
- `machine-specific-paths`: Do not hard-code a person's home directory, private scripts or services into code, commits or the result.
- `commit-foreign-edits`: Do not commit changes this run did not make.
