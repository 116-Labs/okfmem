"""Tests for #128: auto-resolving rebase conflicts on state/<repo>.md in flat-pool stores."""
import json
import os
import shutil
import subprocess
import pytest

import memory_init as mi
import memory_sync as ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")

STATE_REL = "state/demo.md"
T_OLD = "2026-10-27T10:00:00.000Z"
T_NEW = "2026-10-27T12:00:00.000Z"


def _state_md(modified, body, machine="host1"):
    return (
        "---\n"
        "type: state\n"
        "project: demo\n"
        f"modified: {modified}\n"
        f"machine: {machine}\n"
        "---\n\n"
        "# STATE — demo\n\n"
        f"## Summary\n{body}\n"
    )


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed (rc={r.returncode}): {r.stderr}")
    return r


def _config(repo):
    _git(repo, "config", "user.email", "tester@test.local")
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


def _seed_flat_pool_store(tmp_path):
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    store = str(tmp_path / "store")
    _git(str(tmp_path), "clone", "-q", origin, store)
    _config(store)
    _write(store, ".gitignore", ".okfmem-sync.lock\n")
    # Flat-pool layout marker in registry.json
    reg = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {"/path/demo": "demo"},
        "overrides": {},
        "layout": "labels",
    }
    _write(store, "registry.json", json.dumps(reg, indent=2) + "\n")
    _write(store, STATE_REL, _state_md("2026-10-01T00:00:00.000Z", "base state"))
    _write(store, "pages/topic.md", "---\ntype: project\n---\nbase topic\n")
    _commit_all(store, "seed")
    _git(store, "push", "-q", "-u", "origin", "HEAD")

    assert mi.has_layout_marker(store)
    return origin, store


def _remote_commit_and_push(tmp_path, origin, files):
    remote = str(tmp_path / "remote-worker")
    _git(str(tmp_path), "clone", "-q", origin, remote)
    _config(remote)
    for rel, content in files.items():
        _write(remote, rel, content)
    _commit_all(remote, "remote update")
    _git(remote, "push", "-q", "origin", "HEAD")


def test_sync_auto_resolves_flat_pool_state_local_newer_wins(tmp_path, capsys):
    origin, store = _seed_flat_pool_store(tmp_path)

    # Remote writes older update
    _remote_commit_and_push(tmp_path, origin, {
        STATE_REL: _state_md(T_OLD, "REMOTE OLD"),
    })

    # Local writes newer update
    _write(store, STATE_REL, _state_md(T_NEW, "LOCAL NEW"))

    res = ms.sync_store(store, "save local state")
    assert res["conflict"] is False
    assert res["pushed"] is True
    assert "LOCAL NEW" in _read(store, STATE_REL)

    # Check stderr notice
    captured = capsys.readouterr()
    assert "okfmem: auto-resolved conflict on 'state/demo.md'" in captured.err
    assert "local wins" in captured.err
    assert "discarded incoming side" in captured.err
    assert "git show" in captured.err
    assert ":state/demo.md" in captured.err


def test_sync_auto_resolves_flat_pool_state_incoming_newer_wins(tmp_path, capsys):
    origin, store = _seed_flat_pool_store(tmp_path)

    # Remote writes newer update
    _remote_commit_and_push(tmp_path, origin, {
        STATE_REL: _state_md(T_NEW, "REMOTE NEW"),
    })

    # Local writes older update
    _write(store, STATE_REL, _state_md(T_OLD, "LOCAL OLD"))

    res = ms.sync_store(store, "save local state")
    assert res["conflict"] is False
    assert "REMOTE NEW" in _read(store, STATE_REL)

    # Check stderr notice
    captured = capsys.readouterr()
    assert "okfmem: auto-resolved conflict on 'state/demo.md'" in captured.err
    assert "incoming wins" in captured.err
    assert "discarded local side" in captured.err
    assert "git show" in captured.err


def test_sync_auto_resolves_flat_pool_state_tie_prefers_local(tmp_path, capsys):
    origin, store = _seed_flat_pool_store(tmp_path)

    # Same timestamp on both sides
    _remote_commit_and_push(tmp_path, origin, {
        STATE_REL: _state_md(T_NEW, "REMOTE TIE"),
    })

    _write(store, STATE_REL, _state_md(T_NEW, "LOCAL TIE"))

    res = ms.sync_store(store, "save local tie")
    assert res["conflict"] is False
    assert res["pushed"] is True
    assert "LOCAL TIE" in _read(store, STATE_REL)

    captured = capsys.readouterr()
    assert "local wins" in captured.err
