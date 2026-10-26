import argparse
import os
import shutil
import subprocess

import pytest

import memory_graduate as mg
import memory_init as mi
import memory_reindex as mr

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/org/repo.git", "repo"),
        ("https://github.com/org/repo", "repo"),
        ("https://github.com/org/repo/", "repo"),
        ("git@github.com:org/repo.git", "repo"),
        ("ssh://git@host:22/org/repo.git", "repo"),
        ("/srv/git/repo.git", "repo"),
        ("C:\\git\\repo.git", "repo"),
        ("git@host:repo.git", "repo"),
        ("", None),
        (None, None),
    ],
)
def test_repo_name_from_remote_url_formats(url, expected):
    assert mi._repo_name_from_remote(url) == expected


def _git_repo(path, remote=None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
        )
    return os.path.normpath(str(path))


def test_remote_repo_name_is_none_without_origin(tmp_path):
    root = _git_repo(tmp_path / "local-only")
    assert mi._remote_repo_name(root) is None
    assert mi.canonical_project_name(root, {}) == "local-only"


def test_two_checkouts_of_one_remote_share_a_project_name(tmp_path):
    url = "https://github.com/org/canonical-tool.git"
    a = _git_repo(tmp_path / "gaal", url)
    b = _git_repo(tmp_path / "gaal-review", url)
    assert mi.canonical_project_name(a, {}) == "canonical-tool"
    assert mi.canonical_project_name(b, {}) == "canonical-tool"


def test_override_beats_remote_name(tmp_path):
    root = _git_repo(tmp_path / "x", "git@github.com:org/repo.git")
    reg = {"overrides": {root: "pinned"}, "map": {}}
    assert mi.canonical_project_name(root, reg) == "pinned"


def test_remote_name_is_not_recorded_as_an_override(tmp_path, monkeypatch):
    store = tmp_path / "store"
    (store / "projects" / "repo").mkdir(parents=True)
    claude = tmp_path / "claude_projects"
    root = _git_repo(tmp_path / "checkout", "git@github.com:org/repo.git")
    entry = claude / mi.encode_root(root)
    entry.mkdir(parents=True)
    os.symlink(store / "projects" / "repo", entry / "memory")
    monkeypatch.setattr(mi, "decode_root", lambda e: root)
    reg, _ = mi.build_registry(str(store), str(claude))
    assert reg["map"][root] == "repo"
    assert reg["overrides"] == {}


@pytest.fixture
def assoc(tmp_path, monkeypatch):
    store = tmp_path / "store"
    (store / "projects" / "my-tool").mkdir(parents=True)
    claude = tmp_path / "claude_projects"
    claude.mkdir()
    root = _git_repo(tmp_path / "my-tool")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(mi, "_current_git_root", lambda: root)
    return {"store": str(store), "claude": str(claude), "root": root}


def _associate(env):
    subprocess.run(
        ["git", "-C", env["root"], "remote", "add", "origin",
         "https://github.com/org/canonical-tool.git"],
        check=True,
    )


def _migrate(env, **kw):
    return mi.migrate_project_association(
        env["store"], env["claude"], {"map": {}, "overrides": {}},
        kw.pop("dry_run", False), **kw)


def test_no_rename_while_repo_has_no_remote(assoc):
    status, _ = _migrate(assoc, assume_yes=True)
    assert status == "ok"
    assert os.path.isdir(os.path.join(assoc["store"], "projects", "my-tool"))


def test_local_first_repo_renames_store_dir_on_association(assoc):
    page = os.path.join(assoc["store"], "projects", "my-tool", "a.md")
    with open(page, "w", encoding="utf-8") as f:
        f.write("page")
    _associate(assoc)
    status, msg = _migrate(assoc, assume_yes=True)
    assert status == "changed", msg
    projects = os.path.join(assoc["store"], "projects")
    assert not os.path.exists(os.path.join(projects, "my-tool"))
    assert os.path.isfile(os.path.join(projects, "canonical-tool", "a.md"))


def test_dry_run_rename_changes_nothing(assoc):
    _associate(assoc)
    status, msg = _migrate(assoc, dry_run=True)
    assert status == "changed" and "would rename" in msg
    assert os.path.isdir(os.path.join(assoc["store"], "projects", "my-tool"))


def test_non_interactive_without_yes_skips_the_rename(assoc, capsys):
    _associate(assoc)
    status, _ = _migrate(assoc, non_interactive=True)
    assert status == "skip"
    assert os.path.isdir(os.path.join(assoc["store"], "projects", "my-tool"))
    assert "okfmem init --yes" in capsys.readouterr().out


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _collision_setup(env):
    projects = os.path.join(env["store"], "projects")
    new = os.path.join(projects, "canonical-tool")
    os.makedirs(new)
    old = os.path.join(projects, "my-tool")
    for d, same in ((old, "old"), (new, "new")):
        _write(os.path.join(d, "same.md"), same)
    _write(os.path.join(old, "only-old.md"), "kept")
    _write(os.path.join(old, "MEMORY.md"), "- [only-old](only-old.md) — old hook\n")
    _write(os.path.join(new, "MEMORY.md"), "- [same](same.md) — new hook\n")
    _associate(env)
    return old, new


def test_rename_collision_merges_without_clobbering(assoc):
    old, new = _collision_setup(assoc)
    status, msg = _migrate(assoc, assume_yes=True)
    assert status == "changed", msg
    assert not os.path.exists(old)
    assert _read(os.path.join(new, "same.md")) == "new"
    assert _read(os.path.join(new, "same.md.orphan")) == "old"
    assert os.path.isfile(os.path.join(new, "only-old.md"))
    assert _read(os.path.join(new, "MEMORY.md")) == (
        "- [same](same.md) — new hook\n- [only-old](only-old.md) — old hook\n"
    )


def test_merge_whose_old_dir_cannot_be_removed_skips(assoc, monkeypatch):
    old, _ = _collision_setup(assoc)

    def locked(path, *a, **kw):
        raise PermissionError(13, "in use", path)

    monkeypatch.setattr(mi.shutil, "rmtree", locked)
    status, msg = _migrate(assoc, assume_yes=True)
    assert status == "skip"
    assert "could not remove 'my-tool'" in msg and "re-run okfmem init" in msg
    assert os.path.isdir(old)


def test_merge_retried_after_failed_removal_adds_no_duplicates(assoc, monkeypatch):
    old, new = _collision_setup(assoc)
    real_rmtree = shutil.rmtree

    def locked(path, *a, **kw):
        raise PermissionError(13, "in use", path)

    monkeypatch.setattr(mi.shutil, "rmtree", locked)
    assert _migrate(assoc, assume_yes=True)[0] == "skip"
    before = sorted(os.listdir(new))
    index = _read(os.path.join(new, "MEMORY.md"))
    monkeypatch.setattr(mi.shutil, "rmtree", real_rmtree)
    status, msg = _migrate(assoc, assume_yes=True)
    assert status == "changed", msg
    assert "0 adopted, 0 kept as .orphan" in msg
    assert not os.path.exists(old)
    assert sorted(os.listdir(new)) == before
    assert _read(os.path.join(new, "MEMORY.md")) == index


def test_unreadable_destination_page_is_kept_as_orphan_not_a_crash(assoc, monkeypatch):
    old, new = _collision_setup(assoc)

    def unreadable(*a, **kw):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(mi.filecmp, "cmp", unreadable)
    status, msg = _migrate(assoc, assume_yes=True)
    assert status == "changed", msg
    assert _read(os.path.join(new, "same.md.orphan")) == "old"


def _decline_move(env, tmp_path, monkeypatch):
    projects = os.path.join(env["store"], "projects")
    home = tmp_path / "home"
    claude_projects = home / ".claude" / "projects"
    link_dir = claude_projects / mi.encode_root(env["root"])
    link_dir.mkdir(parents=True)
    link = str(link_dir / "memory")
    os.symlink(os.path.join(projects, "my-tool"), link)
    _associate(env)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    answers = iter([True, False])
    monkeypatch.setattr(mi, "_prompt_yes_no", lambda *a, **kw: next(answers))
    monkeypatch.setattr(
        mi, "detect_harnesses", lambda: {
            "claude_code": str(home / ".claude" / "CLAUDE.md"),
            "codex": None,
            "antigravity": None,
        },
    )
    mi.cmd_run(env["store"], dry_run=False, apply_cleanup=False, wire_hook=False)
    return link, str(claude_projects)


def test_declined_rename_keeps_the_memory_link_on_the_old_project(
    assoc, tmp_path, monkeypatch, capsys
):
    projects = os.path.join(assoc["store"], "projects")
    link, _ = _decline_move(assoc, tmp_path, monkeypatch)
    assert "store project move pending" in capsys.readouterr().out
    assert not os.path.exists(os.path.join(projects, "canonical-tool"))
    assert os.path.realpath(link) == os.path.realpath(os.path.join(projects, "my-tool"))


def test_pending_move_resolves_the_old_project_everywhere(
    assoc, tmp_path, monkeypatch
):
    _, claude_projects = _decline_move(assoc, tmp_path, monkeypatch)
    monkeypatch.setattr(mg, "_current_git_root", lambda: assoc["root"])
    store = assoc["store"]
    args = argparse.Namespace(target=None, store=store, project=None)
    assert mr.resolve_target(args) == os.path.join(store, "projects", "my-tool")
    assert mg.resolve_project(store, None) == "my-tool"
    assert mi.resolve_project(store)["project"] == "my-tool"
    assert mi.project_link_state(store, claude_projects) == ("linked", "my-tool")


def test_harness_link_is_repointed_after_rename(assoc):
    projects = os.path.join(assoc["store"], "projects")
    link_dir = os.path.join(assoc["claude"], mi.encode_root(assoc["root"]))
    os.makedirs(link_dir)
    link = os.path.join(link_dir, "memory")
    os.symlink(os.path.join(projects, "my-tool"), link)
    _associate(assoc)
    assert _migrate(assoc, assume_yes=True)[0] == "changed"
    reg = {"map": {}, "overrides": {}}
    status, msg = mi.link_project_memory(
        assoc["store"], assoc["claude"], {"claude_code": "x"}, reg,
        dry_run=False, assume_yes=True)
    assert status == "changed", msg
    assert os.path.realpath(link) == os.path.realpath(
        os.path.join(projects, "canonical-tool"))
