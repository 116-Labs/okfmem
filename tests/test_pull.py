"""Unit coverage for memory_pull.pull_store's fail-open branches.

pull_store is what the #16 SessionStart pull-hook rests on: it must NEVER raise
and must exit non-zero (via main) ONLY on a rebase conflict or a failed or
withheld caller-supplied token (`auth_failed`, `token_clash`, #140). These tests drive
each branch with throwaway git repos + local bare "remotes" so the fail-open
contract is locked in rather than only smoke-verified by hand.
"""
import os
import shutil
import subprocess
import sys

import pytest

import memory_pull as mp

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


# --------------------------------------------------------------------------
# git helpers — real repos, no network
# --------------------------------------------------------------------------

def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed (rc={r.returncode}): "
                             f"{r.stderr}")
    return r


def _config(repo):
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo, name, content):
    os.makedirs(os.path.dirname(os.path.join(repo, name)), exist_ok=True)
    with open(os.path.join(repo, name), "w", encoding="utf-8") as f:
        f.write(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"c-{name}")


def _bare(tmp_path, name="origin.git"):
    origin = str(tmp_path / name)
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    return origin


def _clone(origin, dest):
    _git(os.path.dirname(dest) or ".", "clone", "-q", origin, dest)
    _config(dest)
    return dest


def _seed(tmp_path):
    """Bare origin + a `store` clone that tracks it and is up to date."""
    origin = _bare(tmp_path)
    store = _clone(origin, str(tmp_path / "store"))
    _commit(store, "a.txt", "1")
    _git(store, "push", "-q", "-u", "origin", "HEAD")  # -u sets @{u}
    return origin, store


def _advance_remote(tmp_path, origin, name, content, fname="a.txt"):
    """Push a new commit to `origin` from a throwaway clone."""
    w = str(tmp_path / f"w-{name}")
    _clone(origin, w)
    _commit(w, fname, content)
    _git(w, "push", "-q", "origin", "HEAD")


# --------------------------------------------------------------------------
# pull_store branches
# --------------------------------------------------------------------------

def test_not_a_git_repo(tmp_path):
    plain = str(tmp_path / "plain")
    os.makedirs(plain)
    res = mp.pull_store(plain)
    assert res == {"pulled": False, "up_to_date": False, "conflict": False,
                   "offline": False, "auth_failed": False, "reason": res["reason"]}
    assert "not a git repo" in res["reason"]


def test_no_upstream_configured(tmp_path):
    # Valid remote so `git fetch` SUCCEEDS, but no branch tracking so @{u}
    # fails -> the "no upstream configured" no-op (distinct from offline).
    origin = _bare(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _config(repo)
    _commit(repo, "a.txt", "1")
    _git(repo, "remote", "add", "origin", origin)  # fetchable, but no @{u}
    res = mp.pull_store(repo)
    assert res["up_to_date"] is False
    assert res["pulled"] is False
    assert res["conflict"] is False
    assert res["offline"] is False
    assert "no upstream" in res["reason"]


def test_offline_dead_remote(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _config(repo)
    _commit(repo, "a.txt", "1")
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    res = mp.pull_store(repo, timeout=10)
    assert res["offline"] is True
    assert res["conflict"] is False


def test_up_to_date(tmp_path):
    _origin, store = _seed(tmp_path)
    res = mp.pull_store(store)
    assert res["up_to_date"] is True
    assert res["pulled"] is False
    assert res["conflict"] is False


def test_fast_forward_when_clean(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "ff", "2")  # remote moves ahead
    res = mp.pull_store(store)
    assert res["pulled"] is True
    assert res["conflict"] is False
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "2"


def test_dirty_tree_autostash_never_raises(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "adv", "2", fname="a.txt")
    # Local uncommitted edit to a DIFFERENT file -> dirty tree forces the
    # rebase --autostash path; the edit must survive and nothing must raise.
    with open(os.path.join(store, "local.txt"), "w", encoding="utf-8") as f:
        f.write("wip")
    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert os.path.exists(os.path.join(store, "local.txt"))  # autostash restored
    with open(os.path.join(store, "local.txt"), encoding="utf-8") as f:
        assert f.read() == "wip"


def test_rebase_conflict_exits_clean(tmp_path):
    origin, store = _seed(tmp_path)
    # Remote and local both change a.txt divergently -> rebase conflicts.
    _advance_remote(tmp_path, origin, "remote", "REMOTE", fname="a.txt")
    _commit(store, "a.txt", "LOCAL")
    res = mp.pull_store(store)
    assert res["conflict"] is True
    assert res["pulled"] is False
    # Rebase aborted -> tree left clean, not mid-rebase.
    assert not mp._rebase_in_progress(store)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=store,
                            capture_output=True, text=True)
    assert status.stdout.strip() == ""


def _tracked(store, path):
    return _git(store, "ls-files", "--", path).stdout.strip() == path


def test_identical_untracked_blocker_fast_forwards_and_becomes_tracked(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "new", "same", fname="state.md")
    with open(os.path.join(store, "state.md"), "w", encoding="utf-8") as f:
        f.write("same")
    res = mp.pull_store(store)
    assert res["pulled"] is True
    assert res["conflict"] is False
    assert _tracked(store, "state.md")
    assert _git(store, "status", "--porcelain").stdout.strip() == ""


def test_identical_untracked_blocker_rebases_over_local_commit(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "new", "same", fname="sub/state.md")
    _commit(store, "local.txt", "mine")
    os.makedirs(os.path.join(store, "sub"))
    with open(os.path.join(store, "sub", "state.md"), "w", encoding="utf-8") as f:
        f.write("same")
    res = mp.pull_store(store)
    assert res["pulled"] is True
    assert res["conflict"] is False
    assert _tracked(store, "sub/state.md")
    assert os.path.exists(os.path.join(store, "local.txt"))


def test_differing_untracked_blocker_is_kept_and_named(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "new", "theirs", fname="state.md")
    mine = os.path.join(store, "state.md")
    with open(mine, "w", encoding="utf-8") as f:
        f.write("mine")
    res = mp.pull_store(store)
    assert res["pulled"] is False
    assert "state.md" in res["reason"]
    assert "not a rebase conflict" in res["reason"]
    with open(mine, encoding="utf-8") as f:
        assert f.read() == "mine"
    assert not _tracked(store, "state.md")


def test_mid_rebase_store_skips_blocker_check_and_keeps_files(tmp_path, monkeypatch):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "new", "same", fname="state.md")
    mine = os.path.join(store, "state.md")
    with open(mine, "w", encoding="utf-8") as f:
        f.write("same")
    monkeypatch.setattr(mp, "_rebase_in_progress", lambda _store: True)
    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert "rebase is already in progress" in res["reason"]
    assert os.path.exists(mine)


def test_sync_drops_identical_untracked_blocker_before_commit(tmp_path):
    import memory_sync as ms

    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "new", "same", fname="state.md")
    for name, body in (("state.md", "same"), ("extra.txt", "x")):
        with open(os.path.join(store, name), "w", encoding="utf-8") as f:
            f.write(body)
    res = ms.sync_store(store, "msg")
    assert res["conflict"] is False
    assert _tracked(store, "state.md")
    assert _tracked(store, "extra.txt")


def test_sync_reports_differing_untracked_blocker(tmp_path):
    import memory_sync as ms

    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "new", "theirs", fname="state.md")
    with open(os.path.join(store, "state.md"), "w", encoding="utf-8") as f:
        f.write("mine")
    res = ms.sync_store(store, "msg")
    assert res["committed"] is False
    assert "state.md" in res["reason"]
    assert "not a rebase conflict" in res["reason"]


def test_never_raises_on_garbage_store(tmp_path):
    # A .git that is a file pointing nowhere is malformed; pull_store must
    # still return a dict, never raise.
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as f:
        f.write("gitdir: /nonexistent\n")
    # .git is a file, not a dir -> treated as "not a git repo", clean no-op.
    res = mp.pull_store(repo)
    assert isinstance(res, dict)
    assert res["conflict"] is False


# --------------------------------------------------------------------------
# main() exit-code mapping — conflict, auth_failed and token_clash are the ONLY
# non-zero exits
# --------------------------------------------------------------------------

def _run_main(monkeypatch, canned):
    monkeypatch.setattr(mp, "pull_store", lambda *a, **k: canned)
    monkeypatch.setattr(sys, "argv",
                        ["okfmem", "--quiet", "--store", "/whatever"])
    with pytest.raises(SystemExit) as e:
        mp.main()
    return e.value.code


def test_main_exit_zero_on_offline(monkeypatch):
    code = _run_main(monkeypatch, {"pulled": False, "up_to_date": False,
                                   "conflict": False, "offline": True,
                                   "reason": "offline"})
    assert code == 0


def test_main_exit_zero_on_up_to_date(monkeypatch):
    code = _run_main(monkeypatch, {"pulled": False, "up_to_date": True,
                                   "conflict": False, "offline": False,
                                   "reason": "up to date"})
    assert code == 0


def test_main_exit_one_only_on_conflict(monkeypatch):
    code = _run_main(monkeypatch, {"pulled": False, "up_to_date": False,
                                   "conflict": True, "offline": False,
                                   "reason": "conflict"})
    assert code == 1


# --------------------------------------------------------------------------
# unlinked_repo_notice — the SessionStart reminder to run `okfmem init`
# --------------------------------------------------------------------------

def test_notice_fires_only_when_unlinked(monkeypatch):
    import memory_init as mi

    monkeypatch.setattr(mi, "project_link_state", lambda store: ("unlinked", "proj"))
    msg = mp.unlinked_repo_notice("/any/store")
    assert msg and "okfmem init" in msg and "proj" in msg

    for state in ("linked", "not-a-repo", "no-claude"):
        monkeypatch.setattr(mi, "project_link_state",
                            lambda store, _s=state: (_s, "proj"))
        assert mp.unlinked_repo_notice("/any/store") is None


def test_notice_never_raises(monkeypatch):
    # Fail-open: this runs on the SessionStart path, so a broken probe must
    # degrade to "no reminder", never a traceback that fails the hook.
    import memory_init as mi

    monkeypatch.setattr(mi, "project_link_state",
                        lambda store: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mp.unlinked_repo_notice("/any/store") is None


# --------------------------------------------------------------------------
# stdin handling — an open pipe must never hang `okfmem pull` (#247)
# --------------------------------------------------------------------------

_PULL = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "memory_pull.py")]


def _spawn_pull(store, cwd, **popen_kw):
    return subprocess.Popen([*_PULL, "--quiet", "--store", str(store)], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **popen_kw)


@pytest.mark.os_real
def test_pull_open_idle_stdin_pipe_exits_zero(tmp_path):
    _, store = _seed(tmp_path)
    p = _spawn_pull(store, str(tmp_path), stdin=subprocess.PIPE)
    try:
        assert p.wait(timeout=20) == 0
    finally:
        p.kill()
        p.communicate()


@pytest.mark.os_real
def test_pull_unclosed_partial_stdin_exits_zero(tmp_path):
    _, store = _seed(tmp_path)
    p = _spawn_pull(store, str(tmp_path), stdin=subprocess.PIPE)
    try:
        p.stdin.write(b'{"session_id": "s1", "cw')
        p.stdin.flush()
        assert p.wait(timeout=20) == 0
    finally:
        p.kill()
        p.communicate()


@pytest.mark.os_real
def test_pull_devnull_and_file_stdin_exit_zero(tmp_path):
    _, store = _seed(tmp_path)
    p = _spawn_pull(store, str(tmp_path), stdin=subprocess.DEVNULL)
    assert p.wait(timeout=20) == 0
    p.communicate()
    payload = tmp_path / "in.json"
    payload.write_text("{}", encoding="utf-8")
    with open(payload, "rb") as f:
        p = _spawn_pull(store, str(tmp_path), stdin=f)
        assert p.wait(timeout=20) == 0
        p.communicate()


@pytest.mark.os_real
def test_pull_closed_json_payload_records_session_start(tmp_path, monkeypatch):
    import json as _json

    seen = []
    monkeypatch.setattr(mp, "pull_store", lambda *a, **k: {
        "conflict": False, "reason": "ok"})
    monkeypatch.setattr(mp, "pull_attached", lambda *a, **k: [])
    monkeypatch.setattr(mp, "unlinked_repo_notice", lambda *a, **k: None)
    monkeypatch.setattr(mp, "check_inbox_nudge", lambda *a, **k: None)
    monkeypatch.setattr(mp, "record_session_start",
                        lambda store, sid, cwd=None: seen.append((sid, cwd)))
    r, w = os.pipe()
    os.write(w, _json.dumps({"session_id": "s1", "cwd": "/x"}).encode())
    os.close(w)
    with os.fdopen(r, "r", encoding="utf-8") as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        monkeypatch.setattr(sys, "argv", ["memory_pull", "--quiet",
                                          "--store", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            mp.main()
    assert exc.value.code == 0
    assert seen == [("s1", "/x")]


def test_read_stdin_session_payload_parses_stringio(monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "s1"}'))
    assert mp._read_stdin_session_payload() == {"session_id": "s1"}


@pytest.mark.os_real
def test_stdin_has_data_false_for_idle_pipe_true_for_written():
    r, w = os.pipe()
    try:
        with os.fdopen(r, "rb", closefd=False) as stream:
            assert mp._stdin_has_data(stream, timeout=0.05) is False
            os.write(w, b"x")
            assert mp._stdin_has_data(stream, timeout=0.05) is True
    finally:
        os.close(r)
        os.close(w)
