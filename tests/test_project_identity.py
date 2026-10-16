import os
import shutil
import subprocess

import pytest

import memory_init as mi

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


def test_rename_collision_merges_without_clobbering(assoc):
    projects = os.path.join(assoc["store"], "projects")
    new = os.path.join(projects, "canonical-tool")
    os.makedirs(new)
    old = os.path.join(projects, "my-tool")
    for d, same in ((old, "old"), (new, "new")):
        with open(os.path.join(d, "same.md"), "w", encoding="utf-8") as f:
            f.write(same)
    with open(os.path.join(old, "only-old.md"), "w", encoding="utf-8") as f:
        f.write("kept")
    _associate(assoc)
    status, msg = _migrate(assoc, assume_yes=True)
    assert status == "changed", msg
    assert not os.path.exists(old)
    with open(os.path.join(new, "same.md"), encoding="utf-8") as f:
        assert f.read() == "new"
    with open(os.path.join(new, "same.md.orphan"), encoding="utf-8") as f:
        assert f.read() == "old"
    assert os.path.isfile(os.path.join(new, "only-old.md"))


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
