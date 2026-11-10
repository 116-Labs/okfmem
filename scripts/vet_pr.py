#!/usr/bin/env python3
"""Vet a fork pull request from an outside author: approve CI, close it, or escalate.

    python scripts/vet_pr.py --repo OWNER/REPO --pr N
    python scripts/vet_pr.py --comment-check

`.github/workflows/vet-fork-pr.yml` runs this under `pull_request_target`, which holds a
write token, so the PR is only ever read through the REST API as data: nothing from its
head is checked out, imported or run (#151). `--comment-check` gives
`.github/workflows/moderate-comments.yml` the same spam patterns and account-age gate.

Every rule fails closed: a fetch that fails leaves its input `None`, and `vet()` turns
any `None` into `escalate`, never `approve`.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MIN_ACCOUNT_AGE_DAYS = 30
MAX_CHANGED_LINES = 500
MAX_CHANGED_FILES = 20
LONG_BLOB_CHARS = 200
RUN_LOOKUP_ATTEMPTS = 6
RUN_LOOKUP_DELAY_S = 15
MARKER = "<!-- vet-fork-pr -->"
BOT_LOGIN = "github-actions[bot]"
NEEDS_HUMAN_LABEL = "needs-human"
REJECT_LABEL = "invalid"

# Shared with moderate-comments.yml; matched case-insensitively.
SPAM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"t\.me/",
        r"\b(whats?app|telegram)\b.*\+?\d",
        r"\b(crypto|bitcoin|forex|binary option|airdrop|nft mint)\b",
        r"\b(hack|recover(y)?)\b.*\b(wallet|funds|account|bitcoin)\b",
        r"\b(cheap|buy).{0,20}(followers|likes|views)\b",
        r"(https?://[^\s]+){4,}",
        r"\b(loan|investment).{0,20}(guaranteed|profit)\b",
    )
]

# Mirrors .github/CODEOWNERS (a test keeps them in step), plus the issue's globs so a
# new launcher or installer is sensitive before CODEOWNERS learns its name.
SENSITIVE_PATHS: tuple[str, ...] = (
    "/.github/",
    "/.gaal/",
    "/AGENTS.md",
    "/CLAUDE.md",
    "/.claude/",
    "/skills/",
    "/plugins/",
    "/scripts/",
    "/install.sh",
    "/install.ps1",
    "/uninstall.sh",
    "/uninstall.ps1",
    "/okfmem",
    "/okfmem.cmd",
    "/okfmem.ps1",
    "/okfmem-statusline.sh",
    "/okfmem-statusline.ps1",
    "/memory_sync.py",
    "/memory_pull.py",
    "/memory_uninstall.py",
    "/install.*",
    "/uninstall.*",
    "/okfmem*",
)

LEAK_GATE = "scripts/check-leaks.py"
URL_RE = re.compile(r"https?://", re.IGNORECASE)
BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{%d,}" % (LONG_BLOB_CHARS + 1))
OBFUSCATION_MARKERS: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    (
        "a base64 decode next to exec/eval",
        re.compile(r"base64|b64decode|atob\(", re.IGNORECASE),
        re.compile(r"\b(exec|eval)\b"),
    ),
    (
        "a download piped into a shell",
        re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
        None,
    ),
    (
        "a subprocess call with a URL",
        re.compile(r"\b(subprocess|os\.system|os\.popen)\b"),
        URL_RE,
    ),
    (f"an encoded blob over {LONG_BLOB_CHARS} characters", BLOB_RE, None),
]


def spam_hits(text: str) -> list[str]:
    return [p.pattern for p in SPAM_PATTERNS if p.search(text or "")]


def account_age_days(created_at: str | None, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 86400


def is_sensitive(path: str) -> bool:
    # Case-folded so a case-insensitive checkout (macOS, Windows) cannot sneak past.
    path = path.lower()
    for pattern in SENSITIVE_PATHS:
        pattern = pattern.lstrip("/").lower()
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def added_lines(file: dict) -> list[str]:
    patch = file.get("patch") or ""
    return [line[1:] for line in patch.splitlines() if line.startswith("+")]


def _show(path: str) -> str:
    return "`" + re.sub(r"[`\r\n]", "", path)[:100] + "`"


def _show_paths(paths: list[str], limit: int = 5) -> str:
    shown = ", ".join(_show(p) for p in paths[:limit])
    return shown + (f" (+{len(paths) - limit} more)" if len(paths) > limit else "")


def _has_no_text_diff(file: dict) -> bool:
    # The files API omits `patch` for binary files and for diffs too large to show.
    if "patch" in file or file.get("status") == "removed":
        return False
    return not (file.get("status") == "renamed" and not file.get("changes"))


def _links_only_docs(files: list[dict]) -> bool:
    names = [f.get("filename", "").lower() for f in files]
    if not names or not all(name.endswith(".md") for name in names):
        return False
    lines = [line for f in files for line in added_lines(f) if line.strip()]
    linked = [line for line in lines if URL_RE.search(line)]
    return bool(lines) and len(linked) * 2 > len(lines)


def vet(
    author: dict | None,
    pr: dict | None,
    files: list[dict] | None,
    now: datetime,
) -> tuple[str, list[str]]:
    """Return ("approve" | "reject" | "escalate", reasons).

    `author` is `GET /users/{login}` plus `merged_prs_elsewhere`; `None` = fetch failed.
    """
    if pr is None:
        return "escalate", ["could not read the pull request"]

    hits = spam_hits(f"{pr.get('title') or ''}\n{pr.get('body') or ''}")
    if hits:
        return "reject", [f"title or body matches spam pattern `{h}`" for h in hits]

    age = account_age_days((author or {}).get("created_at"), now)
    young = age is not None and age < MIN_ACCOUNT_AGE_DAYS
    if files is not None and young and _links_only_docs(files):
        return "reject", [
            f"account is {age:.0f} days old and the diff only adds links to Markdown"
        ]

    signals: list[str] = []
    if author is None:
        signals.append("could not read the author's account")
    else:
        if age is None:
            signals.append("could not read the account's creation date")
        elif young:
            signals.append(
                f"account is {age:.0f} days old (under {MIN_ACCOUNT_AGE_DAYS})"
            )
        merged = author.get("merged_prs_elsewhere")
        if merged is None:
            signals.append("could not count the author's merged PRs elsewhere")
        elif merged == 0:
            signals.append("author has no merged pull requests in other projects")
        if author.get("type") != "User":
            signals.append(f"account type is {author.get('type')!r}, not a User")
        if not author.get("public_repos") and not author.get("followers"):
            signals.append("account has no public activity (0 repos, 0 followers)")

    if files is None:
        signals.append("could not read the changed files")
        return "escalate", signals

    expected = pr.get("changed_files")
    if isinstance(expected, int) and len(files) < expected:
        signals.append(f"file list is incomplete ({len(files)} of {expected} files)")

    paths = [f.get("filename", "") for f in files]
    paths += [f["previous_filename"] for f in files if f.get("previous_filename")]
    sensitive = [p for p in paths if is_sensitive(p)]
    if sensitive:
        signals.append(f"touches a CODEOWNERS path: {_show_paths(sensitive)}")
    if LEAK_GATE in paths:
        signals.append(f"changes `{LEAK_GATE}`, which CI runs before anything else")

    for label, first, second in OBFUSCATION_MARKERS:
        found = [
            f.get("filename", "")
            for f in files
            if any(
                first.search(line) and (second is None or second.search(line))
                for line in added_lines(f)
            )
        ]
        if found:
            signals.append(f"adds {label} in {_show_paths(found)}")

    opaque = [f.get("filename", "") for f in files if _has_no_text_diff(f)]
    if opaque:
        signals.append(f"no text diff (binary or too large): {_show_paths(opaque)}")

    changed = sum((f.get("additions") or 0) + (f.get("deletions") or 0) for f in files)
    changed = max(changed, (pr.get("additions") or 0) + (pr.get("deletions") or 0))
    if changed > MAX_CHANGED_LINES:
        signals.append(f"large diff: {changed} lines (over {MAX_CHANGED_LINES})")
    if len(files) > MAX_CHANGED_FILES:
        signals.append(f"large diff: {len(files)} files (over {MAX_CHANGED_FILES})")

    if signals:
        return "escalate", signals

    return "approve", [
        f"account is {age / 365.25:.1f} years old",
        f"author has {merged} merged pull requests in other projects",
        f"{len(files)} files and {changed} changed lines, none on a CODEOWNERS path",
        "no obfuscation, download-and-run or binary content in the diff",
    ]


def comment_spam_reason(
    body: str, created_at: str | None, now: datetime
) -> str | None:
    """Why a comment should be hidden, or None. An unknown age skips the age gate."""
    age = account_age_days(created_at, now)
    if age is not None and age < MIN_ACCOUNT_AGE_DAYS:
        return f"account age {age:.1f}d < {MIN_ACCOUNT_AGE_DAYS}d"
    hits = spam_hits(body)
    if hits:
        return "spam signals: " + ", ".join(hits)
    return None


class ApiError(Exception):
    pass


class GitHub:
    def __init__(self, token: str, base: str = "https://api.github.com") -> None:
        self.token = token
        self.base = base

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "okfmem-vet-pr")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            return json.loads(raw) if raw else None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ApiError(f"{method} {path}: {exc}") from exc


def paged(api, path: str, max_pages: int = 30) -> list:
    sep = "&" if "?" in path else "?"
    items: list = []
    for page in range(1, max_pages + 1):
        chunk = api.request("GET", f"{path}{sep}per_page=100&page={page}")
        if not isinstance(chunk, list):
            raise ApiError(f"GET {path}: expected a list")
        items.extend(chunk)
        if len(chunk) < 100:
            return items
    raise ApiError(f"GET {path}: more than {max_pages} pages")


def _try(what: str, fn, *args):
    try:
        return fn(*args)
    except (ApiError, KeyError, TypeError) as exc:
        print(f"vet-pr: {what} failed: {exc}", file=sys.stderr)
        return None


def _user(api, login: str):
    return api.request("GET", "/users/" + urllib.parse.quote(login))


def _merged_elsewhere(api, owner: str, login: str) -> int | None:
    query = urllib.parse.quote(f"is:pr is:merged author:{login} -user:{owner}")
    found = api.request("GET", f"/search/issues?per_page=1&q={query}")
    total = found["total_count"]
    if not isinstance(total, int) or (found.get("incomplete_results") and not total):
        return None
    return total


def fetch_inputs(api, repo: str, number: int):
    pr = _try("read PR", api.request, "GET", f"/repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict):
        return None, None, None
    author = None
    login = (pr.get("user") or {}).get("login")
    if login:
        user = _try("read author", _user, api, login)
        if isinstance(user, dict):
            owner = repo.split("/")[0]
            merged = _try("search merged PRs", _merged_elsewhere, api, owner, login)
            author = dict(user, merged_prs_elsewhere=merged)
    files = _try("list files", paged, api, f"/repos/{repo}/pulls/{number}/files")
    return author, pr, files


def _awaiting_approval(run: dict) -> bool:
    return "action_required" in (run.get("status"), run.get("conclusion"))


def approve_runs(api, repo: str, sha: str, sleep=time.sleep):
    """Approve the CI runs awaiting approval for `sha`: (approved run ids, error)."""
    path = f"/repos/{repo}/actions/runs?per_page=100&head_sha={urllib.parse.quote(sha)}"
    runs: list[dict] = []
    for attempt in range(RUN_LOOKUP_ATTEMPTS):
        try:
            listing = api.request("GET", path)
        except ApiError as exc:
            return [], f"could not list CI runs: {exc}"
        # This workflow's own pull_request_target run is not CI and must not count.
        runs = [
            r
            for r in (listing or {}).get("workflow_runs") or []
            if r.get("event") == "pull_request"
        ]
        if runs:
            break
        if attempt < RUN_LOOKUP_ATTEMPTS - 1:
            sleep(RUN_LOOKUP_DELAY_S)
    if not runs:
        return [], "no CI run appeared for the head commit, so nothing was approved"

    approved: list[int] = []
    for run in filter(_awaiting_approval, runs):
        try:
            api.request("POST", f"/repos/{repo}/actions/runs/{run['id']}/approve")
        except ApiError as exc:
            return approved, f"could not approve CI run {run['id']}: {exc}"
        approved.append(run["id"])
    return approved, None


def upsert_comment(api, repo: str, number: int, body: str) -> None:
    # Edit only this workflow's own comment: anyone can post the marker text.
    for comment in paged(api, f"/repos/{repo}/issues/{number}/comments"):
        mine = (comment.get("user") or {}).get("login") == BOT_LOGIN
        if mine and MARKER in (comment.get("body") or ""):
            path = f"/repos/{repo}/issues/comments/{comment['id']}"
            api.request("PATCH", path, {"body": body})
            return
    api.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})


HEADINGS = {
    "approve": "CI was approved automatically for this pull request.",
    "escalate": "CI waits for a maintainer to approve it. These checks tripped:",
    "reject": "This pull request was closed automatically as spam:",
}


def comment_body(verdict: str, reasons: list[str], sha: str | None) -> str:
    lines = [MARKER, HEADINGS[verdict], ""]
    if verdict == "approve":
        lines += ["Why:", ""]
    lines += [f"- {r}" for r in reasons]
    if sha:
        lines += ["", f"Vetted head commit `{sha[:12]}`."]
    if verdict == "reject":
        lines += ["", "If this is a mistake, a maintainer can reopen it."]
    return "\n".join(lines) + "\n"


def review_pr(
    api,
    repo: str,
    number: int,
    *,
    now: datetime,
    action: str | None = None,
    sender: str | None = None,
    sleep=time.sleep,
) -> tuple[str, list[str]]:
    author, pr, files = fetch_inputs(api, repo, number)
    verdict, reasons = vet(author, pr, files, now)
    sha = ((pr or {}).get("head") or {}).get("sha")
    pr_author = ((pr or {}).get("user") or {}).get("login")

    if verdict == "reject" and action == "reopened" and sender and sender != pr_author:
        # Never re-close what a maintainer deliberately reopened.
        verdict = "escalate"
        reasons = [f"reopened by @{sender} after an automatic close", *reasons]

    if verdict == "approve":
        if not sha:
            verdict, reasons = "escalate", ["could not read the head commit"]
        else:
            approved, error = approve_runs(api, repo, sha, sleep)
            if error:
                verdict, reasons = "escalate", [error]
            elif approved:
                ids = ", ".join(str(i) for i in approved)
                reasons = [*reasons, f"approved CI runs: {ids}"]
            else:
                reasons = [*reasons, "no CI run was waiting for approval"]

    upsert_comment(api, repo, number, comment_body(verdict, reasons, sha))
    labels = f"/repos/{repo}/issues/{number}/labels"
    if verdict == "escalate":
        api.request("POST", labels, {"labels": [NEEDS_HUMAN_LABEL]})
    elif verdict == "reject":
        api.request("POST", labels, {"labels": [REJECT_LABEL]})
        api.request("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
    return verdict, reasons


def comment_check(api, event: dict, now: datetime, output: str | None) -> str | None:
    comment = event.get("comment") or {}
    login = (comment.get("user") or {}).get("login")
    created_at = None
    if login:
        user = _try("read commenter", _user, api, login)
        created_at = user.get("created_at") if isinstance(user, dict) else None
    reason = comment_spam_reason(comment.get("body") or "", created_at, now)
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"hide={'true' if reason else 'false'}\n")
    print(f"hide @{login}: {reason}" if reason else "no age or content signal")
    return reason


def _load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="OWNER/REPO")
    parser.add_argument("--pr", type=int, help="pull request number")
    parser.add_argument(
        "--comment-check",
        action="store_true",
        help="score the comment in $GITHUB_EVENT_PATH and write hide=true|false",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is not set")
    api = GitHub(token)
    event = _load_event()
    now = datetime.now(timezone.utc)

    if args.comment_check:
        comment_check(api, event, now, os.environ.get("GITHUB_OUTPUT"))
        return 0
    if not args.repo or not args.pr:
        parser.error("--repo and --pr are required")

    try:
        verdict, reasons = review_pr(
            api,
            args.repo,
            args.pr,
            now=now,
            action=event.get("action"),
            sender=(event.get("sender") or {}).get("login"),
        )
    except ApiError as exc:
        print(f"vet-pr: could not act on the verdict: {exc}", file=sys.stderr)
        return 1
    print(verdict)
    for reason in reasons:
        print(f"- {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
