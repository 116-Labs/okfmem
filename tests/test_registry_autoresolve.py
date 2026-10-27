"""Tests for #80: auto-resolving registry.json conflicts during okfmem pull.

Covers:
- Acceptance Criterion 1: A test store with an uncommitted local registry.json root
  and a remote commit adding a different root pulls cleanly; the result holds both roots, sorted.
- Acceptance Criterion 2: A conflict that touches registry.json and some other file
  still aborts and restores the tree, as today.
- Acceptance Criterion 3: After init --yes followed by a remote registry change,
  a worker's next pull succeeds.
- Acceptance Criterion 4: Windows and POSIX roots both pass through the merge untouched.
"""
import json
import os
import shutil
import subprocess
import pytest

import memory_init as mi
import memory_pull as mp
import memory_sync as ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed (rc={r.returncode}): {r.stderr}")
    return r


def _config(repo):
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def _write(repo, rel, content):
    path = os.path.join(repo, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(repo, rel):
    with open(os.path.join(repo, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def _commit_all(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def _seed_store(tmp_path):
    """Create a bare origin and a tracking store clone with an initial registry.json."""
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    store = str(tmp_path / "store")
    _git(str(tmp_path), "clone", "-q", origin, store)
    _config(store)
    _write(store, ".gitignore", ".okfmem-sync.lock\n")
    initial_reg = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {"/initial/path": "initial"},
        "overrides": {},
    }
    _write(store, "registry.json", json.dumps(initial_reg, indent=2) + "\n")
    _write(store, "projects/demo/topic.md", "base topic\n")
    _commit_all(store, "seed")
    _git(store, "push", "-q", "-u", "origin", "HEAD")
    return origin, store


def test_merge_registry_conflict_unit(capsys):
    """Acceptance criterion 4: Windows and POSIX roots both pass through the merge untouched.
    Key collision: incoming wins and local is reported to stderr.
    Sections are sorted."""
    incoming = json.dumps({
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {
            "/posix/projA": "projA",
            "/collision/proj": "incoming-val",
        },
        "overrides": {
            "/posix/overrideA": "overA",
        },
    })
    local = json.dumps({
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {
            "C:\\Windows\\projB": "projB",
            "/collision/proj": "local-val",
        },
        "overrides": {
            "C:\\Windows\\overrideB": "overB",
        },
    })

    merged_str = ms._merge_registry_conflict(incoming, local)
    assert merged_str is not None
    merged = json.loads(merged_str)

    # Both POSIX and Windows roots survived untouched
    assert merged["map"]["/posix/projA"] == "projA"
    assert merged["map"]["C:\\Windows\\projB"] == "projB"
    assert merged["overrides"]["/posix/overrideA"] == "overA"
    assert merged["overrides"]["C:\\Windows\\overrideB"] == "overB"

    # Collision: incoming won
    assert merged["map"]["/collision/proj"] == "incoming-val"

    # Reported to stderr
    captured = capsys.readouterr()
    assert "/collision/proj" in captured.err
    assert "incoming 'incoming-val' wins" in captured.err

    # Sorted keys
    map_keys = list(merged["map"].keys())
    assert map_keys == sorted(map_keys)


def test_pull_uncommitted_registry_root_pulls_cleanly(tmp_path):
    """Acceptance criterion 1: A test store with an uncommitted local registry.json root
    and a remote commit adding a different root pulls cleanly; the result holds both roots, sorted."""
    origin, store = _seed_store(tmp_path)

    # Remote adds root B and pushes
    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    reg_remote = json.loads(_read(remote, "registry.json"))
    reg_remote["map"]["/remote/rootB"] = "rootB"
    _write(remote, "registry.json", json.dumps(reg_remote, indent=2) + "\n")
    _commit_all(remote, "add rootB from remote")
    _git(remote, "push", "-q", "origin", "HEAD")

    # Local store edits registry.json (uncommitted!) to add root C
    reg_local = json.loads(_read(store, "registry.json"))
    reg_local["map"]["/local/rootC"] = "rootC"
    _write(store, "registry.json", json.dumps(reg_local, indent=2) + "\n")

    # Local store pulls
    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True

    # Resulting registry has both roots, sorted
    final_reg = json.loads(_read(store, "registry.json"))
    assert "/initial/path" in final_reg["map"]
    assert "/remote/rootB" in final_reg["map"]
    assert "/local/rootC" in final_reg["map"]
    assert list(final_reg["map"].keys()) == sorted(final_reg["map"].keys())

    # Tree is clean and not mid-rebase
    assert not mp._rebase_in_progress(store)
    status = _git(store, "status", "--porcelain")
    assert status.stdout.strip() == ""


def test_conflict_registry_plus_other_file_aborts(tmp_path):
    """Acceptance criterion 2: A conflict that touches registry.json and some other file
    still aborts and restores the tree, as today."""
    origin, store = _seed_store(tmp_path)

    # Remote modifies registry.json AND projects/demo/topic.md
    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    reg_remote = json.loads(_read(remote, "registry.json"))
    reg_remote["map"]["/remote/rootB"] = "rootB"
    _write(remote, "registry.json", json.dumps(reg_remote, indent=2) + "\n")
    _write(remote, "projects/demo/topic.md", "remote topic content\n")
    _commit_all(remote, "remote conflict")
    _git(remote, "push", "-q", "origin", "HEAD")

    # Local modifies registry.json AND projects/demo/topic.md divergently
    _write(store, "projects/demo/topic.md", "local conflicting topic\n")
    reg_local = json.loads(_read(store, "registry.json"))
    reg_local["map"]["/local/rootC"] = "rootC"
    _write(store, "registry.json", json.dumps(reg_local, indent=2) + "\n")

    # Local pulls — must abort because durable page topic.md is in conflict!
    res = mp.pull_store(store)
    assert res["conflict"] is True
    assert res["pulled"] is False
    assert not mp._rebase_in_progress(store)

    # ...and the tree is restored: local edits back, nothing unmerged or stashed.
    assert _read(store, "projects/demo/topic.md") == "local conflicting topic\n"
    assert "/local/rootC" in json.loads(_read(store, "registry.json"))["map"]
    assert not ms._has_unmerged_paths(store)
    assert _git(store, "stash", "list").stdout.strip() == ""


def test_merge_registry_conflict_refuses_malformed_section():
    good = json.dumps({"map": {"/a": "a"}, "overrides": {"/o": "o"}})
    bad = json.dumps({"map": {"/b": "b"}, "overrides": None})
    assert ms._merge_registry_conflict(bad, good) is None
    assert ms._merge_registry_conflict(good, bad) is None


def test_pull_commits_only_registry_and_leaves_other_staged_paths(tmp_path):
    origin, store = _seed_store(tmp_path)
    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    _write(remote, "projects/demo/other.md", "remote page\n")
    _commit_all(remote, "remote page")
    _git(remote, "push", "-q", "origin", "HEAD")

    reg_local = json.loads(_read(store, "registry.json"))
    reg_local["map"]["/local/rootC"] = "rootC"
    _write(store, "registry.json", json.dumps(reg_local, indent=2) + "\n")
    _write(store, "projects/demo/staged.md", "staged by hand\n")
    _git(store, "add", "--", "projects/demo/staged.md")

    res = mp.pull_store(store)
    assert res["pulled"] is True
    registry_commit = _git(store, "log", "-1", "--name-only", "--format=",
                           "--grep=chore(registry)", "--fixed-strings")
    assert registry_commit.stdout.split() == ["registry.json"]
    assert _git(store, "diff", "--cached", "--name-only").stdout.split() == [
        "projects/demo/staged.md"]


def test_pull_reports_failed_registry_commit(tmp_path):
    origin, store = _seed_store(tmp_path)
    reg_local = json.loads(_read(store, "registry.json"))
    reg_local["map"]["/local/rootC"] = "rootC"
    _write(store, "registry.json", json.dumps(reg_local, indent=2) + "\n")
    # A stale index lock makes the pre-pull add/commit fail.
    _write(store, ".git/index.lock", "")

    res = mp.pull_store(store)
    os.remove(os.path.join(store, ".git", "index.lock"))
    assert "could not commit the pending registry.json" in res["reason"]
    assert _git(store, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert "/local/rootC" in json.loads(_read(store, "registry.json"))["map"]


def test_pull_leaves_registry_alone_during_manual_rebase(tmp_path):
    origin, store = _seed_store(tmp_path)
    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    reg_remote = json.loads(_read(remote, "registry.json"))
    reg_remote["map"]["/remote/rootB"] = "rootB"
    _write(remote, "registry.json", json.dumps(reg_remote, indent=2) + "\n")
    _commit_all(remote, "remote root")
    _git(remote, "push", "-q", "origin", "HEAD")

    reg_local = json.loads(_read(store, "registry.json"))
    reg_local["map"]["/local/rootC"] = "rootC"
    _write(store, "registry.json", json.dumps(reg_local, indent=2) + "\n")
    _commit_all(store, "local root")
    _git(store, "fetch", "-q")
    _git(store, "rebase", "@{u}", check=False)
    assert mp._rebase_in_progress(store)

    res = mp.pull_store(store)
    assert res["pulled"] is False and res["conflict"] is False
    assert mp._rebase_in_progress(store)
    assert ms._unmerged_paths(store) == ["registry.json"]


def _page(body, last_accessed, access_count):
    return ("---\nname: topic\ntype: project\n"
            f"last_accessed: {last_accessed}\naccess_count: {access_count}\n"
            f"---\n\n{body}\n")


def test_merge_bookkeeping_conflict_keeps_content_and_max_counters():
    base = _page("base body", "2026-10-01", 3)
    incoming = _page("remote body", "2026-10-10", 5)
    local = _page("base body", "2026-10-15", 4)
    merged = ms._merge_bookkeeping_conflict(base, incoming, local)
    assert merged == _page("remote body", "2026-10-15", 5)


def test_merge_bookkeeping_conflict_refuses_content_on_both_sides():
    base = _page("base body", "2026-10-01", 3)
    incoming = _page("remote body", "2026-10-10", 5)
    local = _page("local body", "2026-10-15", 4)
    assert ms._merge_bookkeeping_conflict(base, incoming, local) is None


def test_pull_dirty_page_bookkeeping_plus_remote_edit_pulls_cleanly(tmp_path):
    origin, store = _seed_store(tmp_path)
    _write(store, "projects/demo/rollout.md", _page("base body", "2026-10-01", 3))
    _commit_all(store, "add page")
    _git(store, "push", "-q", "origin", "HEAD")

    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    _write(remote, "projects/demo/rollout.md", _page("remote body", "2026-10-10", 5))
    _commit_all(remote, "remote edit")
    _git(remote, "push", "-q", "origin", "HEAD")

    # A Stop-hook bump on the pull-only machine, never synced.
    _write(store, "projects/demo/rollout.md", _page("base body", "2026-10-15", 4))

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert _read(store, "projects/demo/rollout.md") == _page(
        "remote body", "2026-10-15", 5)
    assert not ms._has_unmerged_paths(store)
    assert _git(store, "stash", "list").stdout.strip() == ""

    # The next pull finds nothing to fight over.
    again = mp.pull_store(store)
    assert again["conflict"] is False


def test_pull_dirty_page_with_non_ascii_content_keeps_bytes(tmp_path):
    origin, store = _seed_store(tmp_path)
    base_body = "café — ”quoted”"
    _write(store, "projects/demo/rollout.md", _page(base_body, "2026-10-01", 3))
    _commit_all(store, "add page")
    _git(store, "push", "-q", "origin", "HEAD")

    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    _write(remote, "projects/demo/rollout.md",
           _page(base_body + " ✓", "2026-10-10", 5))
    _commit_all(remote, "remote edit")
    _git(remote, "push", "-q", "origin", "HEAD")

    _write(store, "projects/demo/rollout.md", _page(base_body, "2026-10-15", 4))

    res = mp.pull_store(store)
    assert res["conflict"] is False
    with open(os.path.join(store, "projects", "demo", "rollout.md"), "rb") as f:
        assert f.read() == _page(
            base_body + " ✓", "2026-10-15", 5).encode("utf-8")


def test_show_blob_returns_none_for_non_utf8_blob(tmp_path):
    _, store = _seed_store(tmp_path)
    with open(os.path.join(store, "projects", "demo", "raw.md"), "wb") as f:
        f.write(b"bad \xff byte\n")
    _git(store, "add", "-A")
    assert ms._show_blob(store, 0, "projects/demo/raw.md") is None
    assert ms._show_blob(store, 0, "projects/demo/topic.md") == "base topic\n"


def test_worker_init_yes_then_remote_registry_change_pull_succeeds(tmp_path):
    """Acceptance criterion 3: After init --yes followed by a remote registry change,
    a worker's next pull succeeds."""
    origin, store = _seed_store(tmp_path)

    # Worker runs write_registry as init --yes would, writing local roots into registry.json
    worker_derived = {
        "map": {
            str(tmp_path / "checkouts" / "worker-repo"): "worker-repo",
        },
        "overrides": {},
    }
    mi.write_registry(store, worker_derived, dry_run=False)

    # Meanwhile, remote machine pushes a new root
    remote = str(tmp_path / "remote")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    reg_remote = json.loads(_read(remote, "registry.json"))
    reg_remote["map"]["/mac/Users/you/other-repo"] = "other-repo"
    _write(remote, "registry.json", json.dumps(reg_remote, indent=2) + "\n")
    _commit_all(remote, "push other-repo")
    _git(remote, "push", "-q", "origin", "HEAD")

    # Worker runs okfmem pull
    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True

    # Both roots are present in the worker's registry.json
    final_reg = json.loads(_read(store, "registry.json"))
    assert str(tmp_path / "checkouts" / "worker-repo") in final_reg["map"]
    assert "/mac/Users/you/other-repo" in final_reg["map"]
    assert not mp._rebase_in_progress(store)


def test_is_foreign_root_linux_and_mac_homes():
    linux_home = "/home/tester"
    assert mi._is_foreign_root("/Users/you/code/repo", home=linux_home)
    assert not mi._is_foreign_root("/home/tester/code/repo", home=linux_home)
    assert mi._is_foreign_root("/home/bob/code/repo", home=linux_home)
    assert mi._is_foreign_root("/opt/code/repo", home=linux_home)
    assert mi._is_foreign_root("C:\\Users\\you\\repo", home=linux_home)

    mac_home = "/Users/you"
    assert mi._is_foreign_root("/home/tester/code/repo", home=mac_home)
    assert not mi._is_foreign_root("/Users/you/code/repo", home=mac_home)
    assert mi._is_foreign_root("/Users/bob/code/repo", home=mac_home)
    assert mi._is_foreign_root("/var/code/repo", home=mac_home)
    assert mi._is_foreign_root("C:\\Users\\you\\repo", home=mac_home)

    win_home = "C:\\Users\\tester"
    assert not mi._is_foreign_root("C:\\Users\\tester\\code\\repo", home=win_home)
    assert not mi._is_foreign_root("c:/Users/tester/code/repo", home=win_home)
    assert mi._is_foreign_root("C:\\Users\\bob\\code\\repo", home=win_home)
    assert mi._is_foreign_root("D:\\code\\repo", home=win_home)
    assert mi._is_foreign_root("/home/tester/code/repo", home=win_home)
    assert mi._is_foreign_root("/Users/you/code/repo", home=win_home)


def test_merge_registry_carries_foreign_mac_and_linux_roots():
    existing = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {
            "/Users/you/mac-repo": "mac-repo",
            "/home/tester/linux-repo": "linux-repo",
            "/home/tester/vanished-repo": "vanished-repo",
            "/opt/shared/repo": "shared-repo",
        },
        "overrides": {},
    }

    # On Linux: Mac root and /opt root are foreign and carried through;
    # vanished-repo is under this home and not in derived, so it is pruned (#81, #113).
    linux_derived = {
        "map": {
            "/home/tester/linux-repo": "linux-repo",
            "/home/tester/new-repo": "new-repo",
        },
        "overrides": {},
    }
    merged_linux = mi._merge_registry(existing, linux_derived, home="/home/tester")
    assert merged_linux["map"]["/Users/you/mac-repo"] == "mac-repo"
    assert merged_linux["map"]["/opt/shared/repo"] == "shared-repo"
    assert merged_linux["map"]["/home/tester/linux-repo"] == "linux-repo"
    assert merged_linux["map"]["/home/tester/new-repo"] == "new-repo"
    assert "/home/tester/vanished-repo" not in merged_linux["map"]

    # On Mac: Linux roots are foreign and carried through (#113).
    mac_derived = {
        "map": {
            "/Users/you/mac-repo": "mac-repo",
        },
        "overrides": {},
    }
    merged_mac = mi._merge_registry(existing, mac_derived, home="/Users/you")
    assert merged_mac["map"]["/home/tester/linux-repo"] == "linux-repo"
    assert merged_mac["map"]["/home/tester/vanished-repo"] == "vanished-repo"
    assert merged_mac["map"]["/opt/shared/repo"] == "shared-repo"
    assert merged_mac["map"]["/Users/you/mac-repo"] == "mac-repo"

    # On Windows: POSIX roots and other-drive roots are foreign and carried through (#113).
    win_existing = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {
            "/Users/you/mac-repo": "mac-repo",
            "/home/tester/linux-repo": "linux-repo",
            "C:\\Users\\tester\\win-repo": "win-repo",
            "C:\\Users\\tester\\vanished-repo": "vanished-repo",
            "D:\\other\\repo": "other-repo",
        },
        "overrides": {},
    }
    win_derived = {
        "map": {
            "C:\\Users\\tester\\win-repo": "win-repo",
            "C:\\Users\\tester\\new-repo": "new-repo",
        },
        "overrides": {},
    }
    merged_win = mi._merge_registry(win_existing, win_derived, home="C:\\Users\\tester")
    assert merged_win["map"]["/Users/you/mac-repo"] == "mac-repo"
    assert merged_win["map"]["/home/tester/linux-repo"] == "linux-repo"
    assert merged_win["map"]["D:\\other\\repo"] == "other-repo"
    assert merged_win["map"]["C:\\Users\\tester\\win-repo"] == "win-repo"
    assert merged_win["map"]["C:\\Users\\tester\\new-repo"] == "new-repo"
    assert "C:\\Users\\tester\\vanished-repo" not in merged_win["map"]


def test_merge_registry_preserves_layout_and_unknown_keys():
    existing = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "layout": "labels",
        "custom_marker": {"enabled": True},
        "map": {"/Users/you/mac-repo": "mac-repo"},
        "overrides": {},
    }
    derived = {
        "map": {"/home/tester/linux-repo": "linux-repo"},
        "overrides": {},
    }
    merged = mi._merge_registry(existing, derived, home="/home/tester")
    assert merged["layout"] == "labels"
    assert merged["custom_marker"] == {"enabled": True}
    assert merged["version"] == 1
    assert merged["default_rule"] == "basename(git-root)"
    assert merged["map"]["/Users/you/mac-repo"] == "mac-repo"
    assert merged["map"]["/home/tester/linux-repo"] == "linux-repo"


def test_write_registry_preserves_layout_key_on_disk(tmp_path):
    store = str(tmp_path)
    reg_path = os.path.join(store, "registry.json")
    seed = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "layout": "labels",
        "custom_key": "custom_val",
        "map": {"/Users/you/mac-repo": "mac-repo"},
        "overrides": {},
    }
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(seed, f, indent=2)

    derived = {
        "map": {"/home/tester/linux-repo": "linux-repo"},
        "overrides": {},
    }
    path, changed, merged = mi.write_registry(store, derived, dry_run=False, home="/home/tester")
    assert changed is True
    assert merged["layout"] == "labels"
    assert merged["custom_key"] == "custom_val"

    with open(reg_path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["layout"] == "labels"
    assert on_disk["custom_key"] == "custom_val"
    assert on_disk["map"]["/Users/you/mac-repo"] == "mac-repo"
    assert on_disk["map"]["/home/tester/linux-repo"] == "linux-repo"

    # Second write with same derived data is a no-op
    _, changed_again, _ = mi.write_registry(store, derived, dry_run=False, home="/home/tester")
    assert changed_again is False
