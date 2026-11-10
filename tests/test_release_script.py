"""scripts/release.sh (#169): status / prepare / publish against a temp repo.

Each test builds a throwaway repo with a bare `origin` and puts a fake `gh` first
on PATH, so nothing reaches GitHub. The fake logs every call and answers the
check-runs query with $GH_CI.
"""
import os
import shutil
import stat
import subprocess

import pytest

from conftest import ROOT

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"),
                                reason="release.sh is a maintainer bash tool")

SCRIPT = os.path.join(ROOT, "scripts", "release.sh")

FAKE_GH = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
if args[:1] == ["api"] and "check-runs" in args[1]:
    print(os.environ.get("GH_CI", "success"))
elif args[:2] == ["release", "view"]:
    if "url" in " ".join(args):
        print("https://example.invalid/release")
    else:
        print(os.environ.get("GH_LATEST", ""))
elif args[:2] == ["pr", "create"]:
    print("https://example.invalid/pull/1")
"""


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                 "GIT_CONFIG_NOSYSTEM": "1", "OKFMEM_RELEASE_REPO": "o/r"}.items():
        monkeypatch.setenv(k, v)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", str(bare))
    (work / "scripts").mkdir()
    shutil.copy(os.path.join(ROOT, "scripts", "check-leaks.py"), work / "scripts")
    (work / "VERSION").write_text("0.2.4\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "chore(release): v0.2.4 — base")
    _git(work, "tag", "-a", "v0.2.4", "-m", "v0.2.4")
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "feat(x): add a (#1)")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-q", "-m", "fix(y): fix b (#2)")
    _git(work, "push", "-q", "-u", "origin", "main", "--tags")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "gh.log"
    log.write_text("")
    monkeypatch.setenv("GH_LOG", str(log))
    notes = tmp_path / "notes.md"
    notes.write_text("- added a (#1)\n- fixed b (#2)\n")
    return {"work": work, "bare": bare, "log": log, "notes": notes}


def rel(r, *args, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([os.environ.get("RELEASE_TEST_BASH", "bash"), SCRIPT, *args], cwd=r["work"], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, env=e)


def remote_has(r, ref):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", ref], cwd=r["bare"],
                          capture_output=True).returncode == 0


# ---------------------------------------------------------------- status

def test_status_groups_commits_and_suggests_minor(repo):
    head = _git(repo["work"], "rev-parse", "HEAD")
    p = rel(repo, "status")
    assert p.returncode == 0, p.stderr
    assert "VERSION      0.2.4" in p.stdout
    assert "commits      2 since v0.2.4" in p.stdout
    assert "feat:" in p.stdout and "fix:" in p.stdout
    assert "suggested    minor -> 0.3.0" in p.stdout
    assert _git(repo["work"], "rev-parse", "HEAD") == head


def test_status_suggests_major_on_breaking_marker(repo):
    (repo["work"] / "c.txt").write_text("c\n")
    _git(repo["work"], "add", "c.txt")
    _git(repo["work"], "commit", "-q", "-m", "feat(z)!: drop old flag")
    assert "suggested    major -> 1.0.0" in rel(repo, "status").stdout


# ---------------------------------------------------------------- prepare refusals

@pytest.mark.parametrize("setup,needle", [
    ("dirty", "uncommitted changes"),
    ("ahead", "not even with origin/main"),
    ("branch", "not on main"),
    ("red", "is 'failure', not success"),
    ("old", "is not newer than VERSION"),
    ("tag", "tag v0.3.0 already exists"),
    ("empty", "notes file is empty"),
])
def test_prepare_refuses(repo, setup, needle):
    w, env, version = repo["work"], {}, "minor"
    if setup == "dirty":
        (w / "a.txt").write_text("changed\n")
    elif setup == "ahead":
        (w / "d.txt").write_text("d\n")
        _git(w, "add", "d.txt")
        _git(w, "commit", "-q", "-m", "chore: local only")
    elif setup == "branch":
        _git(w, "switch", "-q", "-c", "other")
    elif setup == "red":
        env["GH_CI"] = "failure"
    elif setup == "old":
        version = "0.2.4"
    elif setup == "tag":
        _git(w, "tag", "-a", "v0.3.0", "-m", "x")
    elif setup == "empty":
        repo["notes"].write_text("  \n")
    head = _git(w, "rev-parse", "HEAD")
    p = rel(repo, "prepare", version, "--notes", str(repo["notes"]), "--yes", env=env)
    assert p.returncode == 1
    assert needle in p.stderr, p.stderr
    assert _git(w, "rev-parse", "HEAD") == head
    assert not remote_has(repo, "refs/heads/release/v0.3.0")


# ---------------------------------------------------------------- prepare

def test_prepare_yes_pushes_branch_and_opens_pr(repo):
    p = rel(repo, "prepare", "minor", "--notes", str(repo["notes"]), "--title", "a and b",
            "--yes")
    assert p.returncode == 0, p.stderr
    w = repo["work"]
    assert (w / "VERSION").read_text() == "0.3.0\n"
    assert _git(w, "rev-parse", "--abbrev-ref", "HEAD") == "release/v0.3.0"
    msg = _git(w, "log", "-1", "--format=%B")
    assert msg == "chore(release): v0.3.0 — a and b"
    assert "Co-Authored-By" not in msg
    assert remote_has(repo, "refs/heads/release/v0.3.0")
    log = repo["log"].read_text()
    assert "pr create --repo o/r --base main --head release/v0.3.0" in log
    assert f"--body-file {repo['notes']}" in log


def test_prepare_without_terminal_or_yes_does_not_push(repo):
    p = rel(repo, "prepare", "0.3.0", "--notes", str(repo["notes"]))
    assert p.returncode == 0, p.stderr
    assert "not pushed" in p.stdout and "git push -u origin release/v0.3.0" in p.stdout
    assert not remote_has(repo, "refs/heads/release/v0.3.0")
    assert "pr create" not in repo["log"].read_text()


def test_prepare_dry_run_writes_nothing(repo):
    head = _git(repo["work"], "rev-parse", "HEAD")
    p = rel(repo, "prepare", "minor", "--notes", str(repo["notes"]), "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "+ git switch -c release/v0.3.0" in p.stdout
    assert "+ git push" in p.stdout and "+ gh pr create" in p.stdout
    assert _git(repo["work"], "rev-parse", "HEAD") == head
    assert (repo["work"] / "VERSION").read_text() == "0.2.4\n"
    assert not remote_has(repo, "refs/heads/release/v0.3.0")
    assert "pr create" not in repo["log"].read_text()


# ---------------------------------------------------------------- publish

def _merge_release(repo, version="0.3.0", title="a and b", after=True):
    """Simulate the merged release PR on origin/main, plus a later commit."""
    w = repo["work"]
    (w / "VERSION").write_text(version + "\n")
    _git(w, "commit", "-q", "-am", f"chore(release): v{version} — {title} (#9)")
    vsha = _git(w, "rev-parse", "HEAD")
    if after:
        (w / "e.txt").write_text("e\n")
        _git(w, "add", "e.txt")
        _git(w, "commit", "-q", "-m", "docs: later commit")
    _git(w, "push", "-q", "origin", "main")
    return vsha


def test_publish_tags_the_version_commit_and_creates_release(repo):
    vsha = _merge_release(repo)
    p = rel(repo, "publish", "0.3.0", "--notes", str(repo["notes"]), "--yes",
            env={"GH_LATEST": "v0.3.0"})
    assert p.returncode == 0, p.stderr
    tagged = subprocess.run(["git", "rev-parse", "v0.3.0^{commit}"], cwd=repo["bare"],
                            capture_output=True, text=True).stdout.strip()
    assert tagged == vsha
    log = repo["log"].read_text()
    assert "release create v0.3.0 --repo o/r --verify-tag --title v0.3.0 — a and b --notes-file" in log


def test_publish_refuses_before_merge(repo):
    p = rel(repo, "publish", "0.3.0", "--notes", str(repo["notes"]), "--yes")
    assert p.returncode == 1
    assert "VERSION is 0.2.4, not 0.3.0" in p.stderr


def test_publish_refuses_existing_tag(repo):
    _merge_release(repo)
    _git(repo["work"], "tag", "-a", "v0.3.0", "-m", "x")
    p = rel(repo, "publish", "0.3.0", "--notes", str(repo["notes"]), "--yes")
    assert p.returncode == 1 and "already exists" in p.stderr


def test_publish_refuses_red_ci(repo):
    _merge_release(repo)
    p = rel(repo, "publish", "0.3.0", "--notes", str(repo["notes"]), "--yes",
            env={"GH_CI": "missing"})
    assert p.returncode == 1 and "is 'missing', not success" in p.stderr
    assert not remote_has(repo, "refs/tags/v0.3.0")


def test_publish_dry_run_writes_nothing(repo):
    _merge_release(repo)
    p = rel(repo, "publish", "0.3.0", "--notes", str(repo["notes"]), "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "+ git tag -a v0.3.0" in p.stdout and "+ gh release create v0.3.0" in p.stdout
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/tags/v0.3.0"],
                          cwd=repo["work"]).returncode != 0
    assert not remote_has(repo, "refs/tags/v0.3.0")
    assert "release create" not in repo["log"].read_text()
