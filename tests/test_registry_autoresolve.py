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
    reg_remote["map"]["/mac/Users/annam/other-repo"] = "other-repo"
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
    assert "/mac/Users/annam/other-repo" in final_reg["map"]
    assert not mp._rebase_in_progress(store)
