import hashlib
import json
import os
import subprocess
import pytest

import memory_pull as mp
import memory_state as ms


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _setup_store(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store, exist_ok=True)

    project_root = str(tmp_path / "demo-repo")
    os.makedirs(project_root, exist_ok=True)
    subprocess.run(["git", "init", "-q", project_root], check=True)

    reg = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {project_root: "demo-repo"},
        "overrides": {},
        "layout": "labels",
    }
    _write_file(os.path.join(store, "registry.json"), json.dumps(reg))

    state_content = (
        "---\n"
        "type: state\n"
        "project: demo-repo\n"
        "modified: 2026-10-27T10:00:00Z\n"
        "machine: host1\n"
        "---\n\n"
        "# STATE — demo-repo\n\n"
        "## Summary\nInitial\n"
    )
    state_file = os.path.join(store, "state", "demo-repo.md")
    _write_file(state_file, state_content)

    return store, project_root, state_file, state_content


def test_state_check_session_baseline(tmp_path):
    store, project_root, state_file, content = _setup_store(tmp_path)

    # Record session baseline
    sess_path = mp.record_session_start(store, "session-1", cwd=project_root)
    assert sess_path is not None

    # Matches baseline
    code, msg = ms.check_state(store, project="demo-repo", session_id="session-1")
    assert code == 0
    assert "matches baseline" in msg

    # Modify state file
    new_content = (
        "---\n"
        "type: state\n"
        "project: demo-repo\n"
        "modified: 2026-10-27T11:00:00Z\n"
        "machine: host1\n"
        "---\n\n"
        "# STATE — demo-repo\n\n"
        "## Summary\nUpdated\n"
    )
    _write_file(state_file, new_content)

    # Now mismatch
    code, msg = ms.check_state(store, project="demo-repo", session_id="session-1")
    assert code != 0
    assert "has changed since baseline" in msg


def test_state_check_since_and_hash_fallback(tmp_path):
    store, _, state_file, content = _setup_store(tmp_path)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Exact match
    code, msg = ms.check_state(
        store,
        project="demo-repo",
        since="2026-10-27T10:00:00Z",
        expected_hash=content_hash,
    )
    assert code == 0

    # Modified timestamp differs
    code, msg = ms.check_state(
        store,
        project="demo-repo",
        since="2026-10-27T09:00:00Z",
        expected_hash=content_hash,
    )
    assert code != 0
    assert "modified" in msg

    # Hash differs
    code, msg = ms.check_state(
        store,
        project="demo-repo",
        since="2026-10-27T10:00:00Z",
        expected_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )
    assert code != 0
    assert "hash" in msg


def test_state_check_is_read_only(tmp_path):
    store, project_root, state_file, content = _setup_store(tmp_path)
    mp.record_session_start(store, "session-read-only", cwd=project_root)

    def file_tree_snapshot(root):
        tree = {}
        for r, _, files in os.walk(root):
            for f in files:
                p = os.path.join(r, f)
                tree[os.path.relpath(p, root)] = (os.path.getmtime(p), os.path.getsize(p))
        return tree

    before = file_tree_snapshot(store)
    code, _ = ms.check_state(store, project="demo-repo", session_id="session-read-only")
    assert code == 0
    after = file_tree_snapshot(store)

    assert before == after


def test_two_windows_one_machine_no_git(tmp_path):
    """AC 5: Two windows on one machine are covered with no git involved:
    a test using two session records on one store, where one window's save
    makes the other's state check fail."""
    store, project_root, state_file, initial_content = _setup_store(tmp_path)

    # Window A starts session A
    mp.record_session_start(store, "window-A", cwd=project_root)

    # Window B starts session B (both see initial state)
    mp.record_session_start(store, "window-B", cwd=project_root)

    # Initially, both state checks pass
    code_a, _ = ms.check_state(store, project="demo-repo", session_id="window-A")
    code_b, _ = ms.check_state(store, project="demo-repo", session_id="window-B")
    assert code_a == 0
    assert code_b == 0

    # Window A performs work and saves state (overwrites state file on disk)
    window_a_save_content = (
        "---\n"
        "type: state\n"
        "project: demo-repo\n"
        "modified: 2026-10-27T10:30:00Z\n"
        "machine: host1\n"
        "---\n\n"
        "# STATE — demo-repo\n\n"
        "## Summary\nWindow A completed task\n"
    )
    _write_file(state_file, window_a_save_content)

    # Window B now checks state before saving
    code_b_after, msg_b_after = ms.check_state(store, project="demo-repo", session_id="window-B")

    # Window B's check must fail, detecting Window A's concurrent save
    assert code_b_after != 0
    assert "has changed since baseline" in msg_b_after


def test_state_check_cli(tmp_path, capsys):
    store, project_root, _, content = _setup_store(tmp_path)
    mp.record_session_start(store, "cli-sess", cwd=project_root)

    with pytest.raises(SystemExit) as exc:
        ms.main(["check", "--store", store, "--project", "demo-repo", "--session", "cli-sess"])
    assert exc.value.code == 0

    with pytest.raises(SystemExit) as exc:
        ms.main(["check", "--store", store, "--project", "demo-repo", "--session", "nonexistent"])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "session record not found" in captured.err
