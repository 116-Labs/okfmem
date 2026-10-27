import hashlib
import json
import os
import subprocess
import time

import memory_init as mi
import memory_pull as mp


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_session_start_record_write_and_schema(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store, exist_ok=True)

    # Initialize a git repo to act as the project root
    project_root = str(tmp_path / "my-project")
    os.makedirs(project_root, exist_ok=True)
    subprocess.run(["git", "init", "-q", project_root], check=True)

    # Set up registry and state file in store
    reg = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {project_root: "my-project"},
        "overrides": {},
        "layout": "labels",
    }
    _write_file(os.path.join(store, "registry.json"), json.dumps(reg))

    state_content = (
        "---\n"
        "type: state\n"
        "project: my-project\n"
        "modified: 2026-10-27T12:00:00Z\n"
        "machine: test-box\n"
        "---\n\n"
        "# STATE — my-project\n\n"
        "## Summary\nInitial state\n"
    )
    state_file = os.path.join(store, "state", "my-project.md")
    _write_file(state_file, state_content)

    expected_hash = hashlib.sha256(state_content.encode("utf-8")).hexdigest()

    # Record session start
    sess_path = mp.record_session_start(store, "sess-abc-123", cwd=project_root)
    assert sess_path is not None
    assert os.path.isfile(sess_path)

    with open(sess_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Check that repo is recorded with modified and hash
    repo_rec = data.get("my-project")
    assert repo_rec is not None
    assert repo_rec["modified"] == "2026-10-27T12:00:00Z"
    assert repo_rec["hash"] == expected_hash

    # Check repos subdict
    assert data["repos"]["my-project"]["modified"] == "2026-10-27T12:00:00Z"
    assert data["repos"]["my-project"]["hash"] == expected_hash


def test_resumed_session_keeps_original_baseline_and_detects_intervening_save(tmp_path):
    import memory_state as mst

    store = str(tmp_path / "store")
    project_root = str(tmp_path / "my-project")
    os.makedirs(project_root, exist_ok=True)
    subprocess.run(["git", "init", "-q", project_root], check=True)
    reg = {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": {project_root: "my-project"},
        "overrides": {},
        "layout": "labels",
    }
    _write_file(os.path.join(store, "registry.json"), json.dumps(reg))
    state_file = os.path.join(store, "state", "my-project.md")
    original = "---\ntype: state\nmodified: 2026-10-27T12:00:00Z\n---\n\n# STATE\n"
    _write_file(state_file, original)

    mp.record_session_start(store, "sess-resume", cwd=project_root)
    # Another session saves while this one is paused, then this one resumes.
    _write_file(state_file, "---\ntype: state\nmodified: 2026-10-27T13:00:00Z\n---\n\n# STATE\nB\n")
    sess_path = mp.record_session_start(store, "sess-resume", cwd=project_root)

    with open(sess_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    expected_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert data["repos"]["my-project"]["modified"] == "2026-10-27T12:00:00Z"
    assert data["repos"]["my-project"]["hash"] == expected_hash
    assert data["my-project"] == data["repos"]["my-project"]

    code, msg = mst.check_state(store, project="my-project", session_id="sess-resume")
    assert code != 0
    assert "changed since baseline" in msg


def test_refresh_session_baseline_allows_a_second_save(tmp_path):
    import memory_state as mst

    store = str(tmp_path / "store")
    project_root = str(tmp_path / "my-project")
    os.makedirs(project_root, exist_ok=True)
    subprocess.run(["git", "init", "-q", project_root], check=True)
    _write_file(os.path.join(store, "registry.json"), json.dumps({
        "version": 1, "default_rule": "basename(git-root)",
        "map": {project_root: "my-project"}, "overrides": {}, "layout": "labels",
    }))
    state_file = os.path.join(store, "state", "my-project.md")
    _write_file(state_file, "---\ntype: state\nmodified: 2026-10-27T12:00:00Z\n---\nA\n")
    mp.record_session_start(store, "sess-repeat", cwd=project_root)
    saved = "---\ntype: state\nmodified: 2026-10-27T13:00:00Z\n---\nB\n"
    _write_file(state_file, saved)

    code, _ = mst.refresh_session_baseline(store, "my-project", "sess-repeat")
    assert code == 0
    code, msg = mst.check_state(store, project="my-project", session_id="sess-repeat")
    assert code == 0, msg


def test_session_records_path_is_gitignored(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store, exist_ok=True)
    gi = os.path.join(store, ".gitignore")

    mi.ensure_store_gitignore(gi, dry_run=False)
    with open(gi, "r", encoding="utf-8") as f:
        content = f.read()

    assert ".okfmem/" in content


def test_prune_sessions_removes_records_older_than_7_days(tmp_path):
    store = str(tmp_path / "store")
    sess_dir = os.path.join(store, ".okfmem", "sessions")
    os.makedirs(sess_dir, exist_ok=True)

    old_file = os.path.join(sess_dir, "old-session.json")
    new_file = os.path.join(sess_dir, "new-session.json")

    _write_file(old_file, json.dumps({"test": 1}))
    _write_file(new_file, json.dumps({"test": 2}))

    now = time.time()
    # 8 days old
    os.utime(old_file, (now - 8 * 86400, now - 8 * 86400))
    # 2 days old
    os.utime(new_file, (now - 2 * 86400, now - 2 * 86400))

    pruned = mp.prune_sessions(store, max_age_days=7, now=now)
    assert pruned == 1

    assert not os.path.exists(old_file)
    assert os.path.exists(new_file)
