"""#138: on Windows a case-variant view folder (`views/OfflineCV` for the store
project `offlinecv`) was never re-spelled, and its link-derived registry entry
flipped against the name-resolved one on every init/pull cycle."""

import importlib.util
import json
import os
import shutil
import subprocess

import pytest

import memory_init as mi

pytestmark = [
    pytest.mark.os_real,
    pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available"),
]

ROLLOUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "rollout-flat-pool.py",
)


def _git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return os.path.normpath(str(path))


def _flat_store(tmp_path, view_spelling="OfflineCV"):
    store = tmp_path / "store"
    (store / "state").mkdir(parents=True)
    (store / "state" / "offlinecv.md").write_text("# STATE\n", encoding="utf-8")
    (store / "pages").mkdir()
    (store / "pages" / "p1.md").write_text(
        "---\nname: p1\ndescription: a page\ntype: project\n"
        "labels: [repo:offlinecv]\n---\n\nbody\n",
        encoding="utf-8",
    )
    (store / "layout.yml").write_text("layout: labels\n", encoding="utf-8")
    view = store / "views" / view_spelling
    view.mkdir(parents=True)
    (view / "MEMORY.md").write_text("# old view\n", encoding="utf-8")
    return str(store)


def _link(claude_projects, root, target):
    entry = os.path.join(claude_projects, mi.encode_root(root))
    os.makedirs(entry, exist_ok=True)
    link = os.path.join(entry, "memory")
    # Windows may make a file symlink when it misjudges a case-variant target.
    os.symlink(target, link, target_is_directory=True)
    return link


@pytest.fixture
def case_insensitive_fs(monkeypatch):
    """Windows-like path handling under `store`: realpath returns the on-disk
    spelling of each component, and a direct case-only rename is refused."""
    real_realpath, real_rename = os.path.realpath, os.rename
    roots = []

    def realpath(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        for root in roots:
            if not resolved.startswith(root + os.sep):
                continue
            cur = root
            for part in os.path.relpath(resolved, root).split(os.sep):
                try:
                    entries = os.listdir(cur)
                except OSError:
                    entries = []
                if part not in entries:
                    same = [e for e in entries if e.casefold() == part.casefold()]
                    part = same[0] if same else part
                cur = os.path.join(cur, part)
            return cur
        return resolved

    def rename(src, dst, *args, **kwargs):
        if src != dst and str(src).casefold() == str(dst).casefold():
            raise FileExistsError(17, "File exists", str(dst))
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(mi.os.path, "realpath", realpath)
    monkeypatch.setattr(mi.os, "rename", rename)
    return lambda store: roots.append(real_realpath(store))


@pytest.fixture
def offlinecv(tmp_path, monkeypatch):
    store = _flat_store(tmp_path)
    claude_projects = str(tmp_path / "claude_projects")
    root = _git_repo(tmp_path / "repos" / "offlinecv")
    link = _link(claude_projects, root, os.path.join(store, "views", "OfflineCV"))
    monkeypatch.setattr(mi, "_current_git_root", lambda: root)
    return {"store": store, "claude": claude_projects, "root": root, "link": link}


def _link_project(env, dry_run=False):
    return mi.link_project_memory(
        env["store"], env["claude"], {"claude_code": "x"},
        {"map": {}, "overrides": {}}, dry_run=dry_run, assume_yes=True)


def test_build_registry_maps_a_case_variant_view_to_the_store_spelling(
    tmp_path, monkeypatch
):
    store = _flat_store(tmp_path, view_spelling="offlinecv")
    claude_projects = str(tmp_path / "claude_projects")
    root = _git_repo(tmp_path / "repos" / "offlinecv")
    _link(claude_projects, root, os.path.join(store, "views", "offlinecv"))
    real_realpath = os.path.realpath
    wanted = os.path.join("views", "offlinecv")

    def on_disk_spelling(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        if resolved.endswith(os.sep + wanted):
            return resolved[: -len("offlinecv")] + "OfflineCV"
        return resolved

    monkeypatch.setattr(mi.os.path, "realpath", on_disk_spelling)
    monkeypatch.setattr(mi, "decode_root", lambda entry: root)

    reg, _ = mi.build_registry(store, claude_projects)

    assert reg["map"] == {root: "offlinecv"}
    assert reg["overrides"] == {}


def test_init_respells_a_case_variant_view_and_repoints_its_link(
    offlinecv, case_insensitive_fs
):
    case_insensitive_fs(offlinecv["store"])
    views = os.path.join(offlinecv["store"], "views")

    status, msg = _link_project(offlinecv)

    assert status == "changed", msg
    assert os.listdir(views) == ["offlinecv"]
    assert os.path.basename(os.readlink(offlinecv["link"])) == "offlinecv"
    assert os.path.isfile(os.path.join(views, "offlinecv", "MEMORY.md"))
    assert _link_project(offlinecv)[0] == "ok"


def test_dry_run_reports_the_respell_and_changes_nothing(offlinecv):
    status, msg = _link_project(offlinecv, dry_run=True)

    assert status == "changed"
    assert "would rename view 'OfflineCV' to 'offlinecv'" in msg
    assert os.listdir(os.path.join(offlinecv["store"], "views")) == ["OfflineCV"]
    assert os.path.basename(os.readlink(offlinecv["link"])) == "OfflineCV"


def test_failed_respell_restores_the_folder_and_skips(offlinecv, monkeypatch):
    real_rename = os.rename

    def second_step_fails(src, dst, *args, **kwargs):
        if os.path.basename(dst) == "offlinecv":
            raise PermissionError(13, "in use", dst)
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(mi.os, "rename", second_step_fails)

    status, msg = _link_project(offlinecv)

    assert status == "skip"
    assert "could not rename view 'OfflineCV' to 'offlinecv'" in msg
    assert os.listdir(os.path.join(offlinecv["store"], "views")) == ["OfflineCV"]


def test_respell_repoints_another_checkouts_link_to_the_variant(offlinecv):
    other = _link(offlinecv["claude"], os.path.join(os.path.dirname(offlinecv["root"]), "wt"),
                  os.path.join(offlinecv["store"], "views", "OfflineCV"))

    status, msg = _link_project(offlinecv)

    assert status == "changed", msg
    assert "repointed 1 other link(s) to offlinecv" in msg
    assert os.path.basename(os.readlink(other)) == "offlinecv"
    assert os.path.isfile(os.path.join(other, "MEMORY.md"))


def _filesystem_is_case_insensitive(path):
    probe = path / "Probe"
    probe.write_text("", encoding="utf-8")
    return (path / "probe").exists()


def test_a_variant_that_is_its_own_store_project_is_not_renamed(offlinecv, tmp_path):
    if _filesystem_is_case_insensitive(tmp_path):
        pytest.skip("two store projects differing only in case cannot coexist here")
    state = os.path.join(offlinecv["store"], "state")
    with open(os.path.join(state, "OfflineCV.md"), "w", encoding="utf-8") as f:
        f.write("# STATE\n")

    assert mi._view_case_variant(offlinecv["store"], "offlinecv") is None


def test_two_init_runs_leave_the_registry_unchanged(tmp_path, monkeypatch):
    store = _flat_store(tmp_path)
    home = tmp_path / ".isolated-home"
    claude_projects = str(home / ".claude" / "projects")
    offlinecv_root = _git_repo(tmp_path / "repos" / "offlinecv")
    _link(claude_projects, offlinecv_root, os.path.join(store, "views", "OfflineCV"))
    registry = os.path.join(store, "registry.json")
    with open(registry, "w", encoding="utf-8") as f:
        json.dump({"map": {offlinecv_root: "offlinecv"}, "overrides": {}}, f)
    # init runs from another repo, as rollout's engine-dir step does: the
    # offlinecv entry then comes from its link alone.
    other = _git_repo(tmp_path / "repos" / "other")
    monkeypatch.setattr(mi, "_current_git_root", lambda: other)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(mi, "detect_harnesses", lambda: {
        "claude_code": str(home / ".claude" / "CLAUDE.md"),
        "codex": None,
        "antigravity": None,
    })

    snapshots = []
    for _ in range(2):
        mi.cmd_run(store, dry_run=False, apply_cleanup=False, wire_hook=False,
                   assume_yes=True)
        with open(registry, encoding="utf-8") as f:
            snapshots.append(f.read())

    assert json.loads(snapshots[0])["map"][offlinecv_root] == "offlinecv"
    assert snapshots[1] == snapshots[0]


def _rollout(monkeypatch, store):
    spec = importlib.util.spec_from_file_location("rollout_flat_pool", ROLLOUT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STORE", store)
    return module


def test_rollout_flags_a_non_canonical_link_target(tmp_path, monkeypatch):
    store = _flat_store(tmp_path)
    (tmp_path / "store" / "state" / "other.md").write_text("# STATE\n", encoding="utf-8")
    (tmp_path / "store" / "views" / "other").mkdir()
    (tmp_path / "store" / "views" / "other" / "MEMORY.md").write_text("# view\n")
    claude_projects = str(tmp_path / "claude_projects")
    miscased = _link(claude_projects, str(tmp_path / "a"),
                     os.path.join(store, "views", "OfflineCV"))
    canonical = _link(claude_projects, str(tmp_path / "b"),
                      os.path.join(store, "views", "other"))
    rollout = _rollout(monkeypatch, store)
    known = mi._store_project_names(store)

    assert rollout.link_flag(miscased, known) == "CASE"
    assert rollout.link_flag(canonical, known) == "OK"


def test_rollout_flags_a_link_still_spelled_after_its_folder(
    tmp_path, monkeypatch, case_insensitive_fs
):
    store = _flat_store(tmp_path, view_spelling="offlinecv")
    case_insensitive_fs(store)
    link = _link(str(tmp_path / "claude_projects"), str(tmp_path / "a"),
                 os.path.join(store, "views", "OfflineCV"))
    rollout = _rollout(monkeypatch, store)

    assert rollout.link_flag(link, mi._store_project_names(store)) == "CASE"
