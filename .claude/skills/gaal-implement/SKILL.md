---
name: gaal-implement
description: Implement one qualified GitHub issue in 116-Labs/okfmem as a verified change on its own `gaal/` branch holding exactly one commit. Use when the dispatcher hands over a repo and issue number that passed qualification and asks for the implement step, headless, with GAAL_RUN_ID and GAAL_RUN_DIR set. Ends by writing `$GAAL_RUN_DIR/result.json` on every exit path. Do not use to open the PR, respond to review, review a PR, merge, or deploy; the PR step belongs to `open-pr`.
---
<!-- gaal-stamp blueprint=implement@1.0.0 shared=1.1.0 profile=424ba91531b1fc7c generated=2026-09-29 content=e9421fd8c5034c86 -->

# gaal-implement

Turn one qualified issue in `116-Labs/okfmem` into a branch off `main` holding **exactly one commit** that satisfies the issue's acceptance criteria and passes every gate. `open-pr` takes it from there. This run is headless: it cannot ask anyone anything (`ask-mid-run`), so ambiguity ends as `needs-clarification`.

## Inputs

- Repo (`116-Labs/okfmem`) and issue number, from dispatch.
- Base ref: `main`, or a parent branch when the dispatcher says the work is stacked. No stacked-branch tool is named in the profile, so fall back to a single branch off the given base.
- Environment: `GAAL_RUN_ID`, `GAAL_RUN_DIR`.
- Project facts: tracker `github`; gate `test` = `python3 -m pytest -q` (required); preflight `leaks` = `python3 scripts/check-leaks.py`; commit convention `conventional`; attribution policy `pr-provenance`; branch prefix `gaal/`; merge method `squash` through a merge queue, with the message taken from the commits; attempt limit 3.

## Before anything else

Record the start time (UTC, RFC 3339) for `started_at`. Keep three running records for the whole run: the **manifest** (every path this run wrote), the **gate log** (name, exact command, exit code, duration in ms for every gate or preflight actually run), and the **attempt counter**. Every exit path below ends at the final step, which writes the result file.

## Steps

1. **Resolve repo and issue.** Confirm the repo and issue number exist by fetching the issue with `gh issue view <N> --repo 116-Labs/okfmem --json number,title,body,state,labels`. Treat an error, auth expiry or rate limit as a failure, never as "no issue" (`fail-closed-reads`). If the repo or issue cannot be resolved, end as `failed` (unreachable) or `needs-clarification` (issue number does not exist or is not this repo's). Never guess.

2. **Read the whole spec.** Read the issue body and **every comment**. Fetch comments with `gh api --paginate repos/116-Labs/okfmem/issues/<N>/comments` so the listing is complete or the run stops (`complete-listings`). Comments that clarify or narrow scope are part of the spec; comments starting with `**Clarification**` are load-bearing. When a comment says the body was rewritten (`**Clarification (rewrite)**` or `(scope narrow)`), the current body wins over older comments.

3. **Check for drift.** Open every file, symbol and line the issue references and compare with the current tree at the base ref. Record each drift. Drift that changes what the plan can safely assume ends the run as `needs-clarification`.

4. **Reuse before building.** Search the repo (`grep`, existing `memory_*.py` modules, `scripts/`) for code that already owns the capability the issue adds, and extend it rather than writing a parallel one. The core is pure Python 3.11+ standard library, so add no dependency. If building new is justified, write down why.

5. **Write the plan first (`plan-before-code`).** Before the first source edit, write a short plan: files to change, tests to add, and for each acceptance criterion how it will be shown to hold. Every criterion maps to part of the plan. Keep the plan in the run directory or the final message, not in the repo. If the issue is ambiguous, or the drift from step 3 is too large for the plan to assume, end as `needs-clarification` with specific questions, each naming the file or criterion it concerns.

6. **Check risky surfaces (guards).** End as `needs-human` instead of changing anything silently if the plan would: break a public contract, persisted page format or store layout without the issue explicitly asking for it (`unapproved-breaking-change`); weaken the repo's confirmation discipline (outward or user-config mutations need a `[y/N]` confirm, destructive ops need typed confirmation, rung-1 ops never prompt); or need a deploy, publish or migration against a shared environment (`deploy`), in which case return the commands for a human instead of running them. Repo hard rules also apply to everything written: no private strings in tracked files (no real home paths, private session URLs, personal emails), nothing under `internal/`.

7. **Look for resumable work, then isolate.**
   - List existing local and remote branches and worktrees matching `gaal/*` for this issue. Resume one only if its state is sound: based on the current base, and its diff is exactly what this issue calls for. Otherwise start fresh.
   - Inspect `git status` in the checkout. If it holds uncommitted or untracked edits this run did not make, do not touch, commit, revert or reformat them (`absorb-stray-edits`). Work in a separate worktree. If ownership of the edits is unclear and a separate worktree cannot avoid the overlap, end as `needs-human`.
   - Create the branch off the base as `gaal/<issue-number>-<short-slug>` (`git worktree add -b gaal/<issue-number>-<short-slug> <path> <base>`), or `git switch -c` in the main checkout only if the dispatcher asked for that. Never commit or work on `main` itself (`base-untouched`). If work was accidentally started on the base, move it to the branch and reset the base to its remote.
   - The core needs no dependency install. If the issue needs test tooling that is missing (for example `pytest`), install it in the branch's environment; if that cannot work, end as `failed` with enough detail to retry.

8. **Implement.** Follow the plan. Read each file before changing it, keep edits minimal, and add or update tests for each acceptance criterion. Add every path you write to the manifest as you write it. Do not fix pre-existing debt outside the change's scope (`unrelated-refactor`); note it for the final message. Keep shipped `.ps1` files pure ASCII if you touch any. A path missing from the manifest is real damage: in stacked work it slides into the next issue's commit.

9. **Preflight and gates, counted (`gates-green`, `bounded-attempts`).** One attempt is one full cycle of implement/fix then gates. Increment the counter at the start of each cycle.
   1. Stage the manifest by explicit path (`git add -- <path> ...`, see step 11) so the tracked-file leak scan sees new files too.
   2. Run the preflight `python3 scripts/check-leaks.py` and the required gate `python3 -m pytest -q`. Capture each exit code and duration, and check the exit status directly, never through a pipe or filter that could hide it (`status-preserved`). Add each to the gate log.
   3. If any exits non-zero, read the output, fix, update the manifest and re-run both from the start. The gates must run on the final tree, so any edit after a green run means running them again.
   4. When the counter reaches **3** and a gate still fails, stop and end as `needs-human` with the failing gate output summarized in the final message and a one-sentence `reason`. Never loop past 3.
   Report validation exactly: a gate that did not run is absent from the log, not passed (`truthful-report`).

10. **Self-review before hand-off.** Nothing is pushed by this step, so review now: read `git diff --cached` in full and check it against the plan and each acceptance criterion, marking each **met** or **not met**. Confirm no unrelated reformatting, no foreign edits, no private strings, no leftover debug output. Fix problems and go back to step 9 if anything changed.

11. **Stage exactly the manifest (`explicit-staging`, `manifest-matches-staged`).** Stage only manifest paths, by explicit path. Never use `git add -A`, `git add .` or `git commit -a`. Compare `git diff --cached --name-only` with the manifest; they must match exactly. Confirm no other change remains that this run is responsible for. Unstaged edits that were there before the run stay untouched.

12. **Create the single commit (`issue-trailer`).** Write one message that describes the change as a whole, in Conventional Commits form (`type(scope): summary`), with a body only when the reason is not obvious. Reference the issue: use a closing keyword (`Resolves #<N>`) only if every acceptance criterion is **met**; if any is not met, use a plain reference (`Refs #<N>`). Follow the attribution policy `pr-provenance`: the commit message carries no model trailer, no `Co-Authored-By` naming a model, no session URL, no generated-with badge (`attribution-policy`); provenance belongs in the PR body, which `open-pr` writes. Never pass `--no-verify`. If a repo hook refuses the commit, read its actual stderr and fix the cause.

13. **Collapse if needed (`single-commit`).** Count with `git rev-list --count <base>..HEAD`. If it is 1, done. If it is more than 1, run the shared collapse routine in place: merge method is `squash` with `merge.message_source: commits`, so collapsing applies. Record the tree hash, reset softly to the merge base, recommit the same content as one commit with a whole-change message (drop process commits such as "wip" or "fix lint"), and verify the new tree hash equals the recorded one. If the rewrite nets to an empty change, restore the previous HEAD and stop. Never use an interactive rebase. Nothing is pushed in this step, so no lease is needed. If the count is 0 (no change was needed), do not fabricate a commit; end as `needs-human` with that finding. Confirm the count is exactly 1 before continuing.

14. **Final checks.** Capture the 40-character `commit_sha` from `git rev-parse HEAD`. Re-run `git rev-list --count <base>..HEAD` (must be 1) and confirm the working tree is clean of changes this run is responsible for. Do not push, open a PR, merge or deploy; those belong to other steps.

15. **Write the run result (`run-result-written`).** This step runs on **every** exit path, including `failed`, `needs-human` and `needs-clarification`. Compose the JSON, write it to `$GAAL_RUN_DIR/result.json.tmp`, then `mv` it to `$GAAL_RUN_DIR/result.json` so the write is atomic. If `GAAL_RUN_DIR` is unset, say so in the final message.

    Required fields:
    - `schema_version`: `1`
    - `run_id`: the value of `$GAAL_RUN_ID`
    - `blueprint`: `"implement"`
    - `blueprint_version`: `"1.0.0"`
    - `repo`: `"116-Labs/okfmem"`
    - `issue`: the issue number as an integer, or `null` if it could not be resolved
    - `pr`: `null`
    - `status`: one of `done`, `needs-human`, `needs-clarification`, `failed`
    - `attempts`: gate-fix cycles consumed, as an integer of at least 1
    - `gates`: every gate or preflight actually run on the final tree, each `{name, command, exit_code, duration_ms}` (for example `test` / `python3 -m pytest -q`, and `leaks` / `python3 scripts/check-leaks.py`); an empty array if none ran
    - `branch`: the branch name, or `null` if none was created
    - `commit_sha`: the 40-hex sha, or `null` if there is no commit
    - `started_at`, `finished_at`: RFC 3339 UTC timestamps

    Conditional fields:
    - `reason`: required unless status is `done`; one sentence of at most 160 characters naming the decision or action needed. Detail goes in the final message.
    - `questions`: required and non-empty when status is `needs-clarification`; each a specific, answerable string naming the file or criterion.

    No other properties are allowed. Never claim "fixed in `<sha>`" unless that sha contains the fix. After writing, finish with a short final message: status, branch, sha, each acceptance criterion as met or not met, gates run, drift found, pre-existing debt noticed but left alone, and any commands a human must run.

## Exit states

- `done`: the single commit exists, `git rev-list --count <base>..HEAD` is 1, and every required gate and the preflight passed on the final tree. `branch`, `commit_sha` and `gates` are set. A partial result (some criterion not met) still ends `done` if gates pass, but uses a plain issue reference, not a closing keyword, and says so in the final message.
- `needs-clarification`: the spec is ambiguous, or the code drifted from what the issue describes. `questions` are specific and answerable and each names the file or criterion it concerns. No commit is made.
- `needs-human`: the attempt limit (3) was reached with a failing gate; a risky-surface guard fired; the checkout had edits whose ownership is unclear; the change nets to nothing; or a stacked rebase conflicted. `reason` says which and what a human should do.
- `failed`: environment or tooling problems (repo unreachable, auth or rate-limit errors, dependency install broken, disk). `reason` includes enough to retry.

## Invariants

- `plan-before-code`: a written plan exists before the first source edit, and every acceptance criterion maps to part of it (step 5).
- `single-commit`: at hand-off the branch has exactly one commit ahead of the base (steps 13 and 14).
- `manifest-matches-staged`: the files in the commit are exactly the manifest of paths this run wrote (steps 8 and 11).
- `gates-green`: every required gate ran on the final tree and exited 0, and the result lists each one (step 9).
- `issue-trailer`: the commit message references the issue, with a closing keyword only when every acceptance criterion is met (step 12).
- `bounded-attempts`: gate-fix cycles stop at 3, then the run ends as `needs-human` (step 9).
- `explicit-staging`: stage only paths this run wrote, by explicit path (steps 9 and 11).
- `base-untouched`: never commit or push to `main` (step 7).
- `fail-closed-reads`: errors, auth expiry and rate limits stop the run and never become "nothing found" (steps 1 and 2).
- `complete-listings`: comment listings are paginated to the end or the run stops (step 2).
- `truthful-report`: the report and result describe what actually happened; a gate that did not run is absent (steps 9 and 15).
- `status-preserved`: no command's failure is lost to a pipe, filter or guard (step 9).
- `attribution-policy`: commit messages follow `pr-provenance` exactly; no AI attribution in the commit (step 12).
- `run-result-written`: `result.json` is written atomically on every exit path (step 15).

## Forbidden actions

- `absorb-stray-edits`: committing, reverting or reformatting changes that were in the checkout before the run.
- `deploy`: running deploys, migrations against shared environments, or publishes; return the commands for a human.
- `unapproved-breaking-change`: breaking a public contract, schema or persisted format unless the issue explicitly asks.
- `unrelated-refactor`: fixing pre-existing debt outside the change's scope; record it in the report.
- `ask-mid-run`: waiting on interactive input; end as `needs-clarification` with questions instead.
- `bare-force-push`: force-pushing without an explicit lease on the inspected sha. This step does not push; if a force-push ever becomes necessary, use only `--force-with-lease=<branch>:<sha>`, never `--force` or `-f`.
- `admin-bypass`: merging, pushing or rewriting with admin privileges to get around branch protection, the merge queue or a verification hook. Never use `--no-verify` or `--admin`.
- `machine-specific-paths`: hard-coding a person's home directory, private scripts or services into commits, code or this run's output.
- `commit-foreign-edits`: committing changes this run did not make.
