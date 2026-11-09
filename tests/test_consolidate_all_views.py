"""Consolidate recompiles every views/* dir, and verify checks pages/ links (#142)."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import memory_consolidate as mc
import memory_init as mi
import memory_reindex as mr
from test_views_projection import _make_repo, _make_store, _write_page


def _set_registry(store, mapping, overrides=None):
    with open(os.path.join(store, "registry.json"), "w", encoding="utf-8") as f:
        json.dump({"map": mapping, "overrides": overrides or {}, "aliases": {}}, f)


def _view(store, name):
    return Path(store, "views", name, "MEMORY.md").read_text(encoding="utf-8")


@pytest.fixture
def two_views(tmp_path):
    store = _make_store(tmp_path / "store")
    repo_a = _make_repo(tmp_path / "a", remote="https://github.com/acme/a.git",
                        markers=["pyproject.toml"])
    repo_b = _make_repo(tmp_path / "b")
    _set_registry(store, {repo_a: "a", repo_b: "b"})
    _write_page(store, "old-a", ["repo:a"])
    _write_page(store, "org-page", ["org:acme"])
    mi.resolve_project(store, cwd=repo_a, compile=True)
    mi.resolve_project(store, cwd=repo_b, compile=True)
    return store, repo_a, repo_b


def test_refresh_drops_archived_page_from_other_repos_view(two_views):
    store, _, repo_b = two_views
    assert "pages/old-a.md" in _view(store, "a")
    os.remove(os.path.join(store, "pages", "old-a.md"))
    _write_page(store, "old-a", ["repo:a"], archive=True)

    mc._refresh_view(store, {"cwd": repo_b})

    assert "pages/old-a.md" not in _view(store, "a")


def test_refresh_adds_new_page_to_other_repos_view(two_views):
    store, _, repo_b = two_views
    _write_page(store, "new-a", ["repo:a"])

    mc._refresh_view(store, {"cwd": repo_b})

    assert "pages/new-a.md" in _view(store, "a")


def test_non_cwd_view_with_local_root_keeps_org_section(two_views):
    store, _, repo_b = two_views
    assert "pages/org-page.md" in _view(store, "a")

    mc._refresh_view(store, {"cwd": repo_b})

    assert "pages/org-page.md" in _view(store, "a")


def test_view_without_local_root_compiles_repo_tier_only(two_views, tmp_path):
    store, _, repo_b = two_views
    _write_page(store, "ghost-page", ["repo:ghost"])
    _write_page(store, "ghost-org", ["org:acme"])
    mi.compile_view(store, "ghost")
    _set_registry(store, {repo_b: "b"})

    mc._refresh_view(store, {"cwd": repo_b})

    text = _view(store, "ghost")
    assert "pages/ghost-page.md" in text
    assert "ghost-org" not in text


def test_dirty_tree_soft_skip_refreshes_every_view(two_views, monkeypatch, capsys):
    store, _, repo_b = two_views
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.com",
                  "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", store, *args], check=True)
    Path(store, "dirty.txt").write_text("x", encoding="utf-8")
    os.remove(os.path.join(store, "pages", "old-a.md"))
    monkeypatch.setattr(sys, "argv",
                        ["consolidate", "--store", store, "--stdin-hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": repo_b})))

    with pytest.raises(SystemExit) as exc:
        mc.main()

    assert exc.value.code == 0
    assert "pages/old-a.md" not in _view(store, "a")


def _linked_view(tmp_path, copy):
    store = _make_store(tmp_path / "store")
    _write_page(store, "kept", ["repo:a"])
    _write_page(store, "gone", ["repo:a"])
    mi.compile_view(store, "a")
    view = Path(store, "views", "a")
    if copy:
        shutil.rmtree(view / "pages", ignore_errors=True)
        if (view / "pages").is_symlink():
            (view / "pages").unlink()
        shutil.copytree(os.path.join(store, "pages"), view / "pages")
        os.remove(view / "pages" / "gone.md")
    else:
        os.remove(os.path.join(store, "pages", "gone.md"))
    return store, view


@pytest.mark.parametrize("copy", [False, True])
def test_verify_flags_dangling_link_into_pages(tmp_path, copy):
    _, view = _linked_view(tmp_path, copy)

    scan = mr.Scan(str(view))

    assert [(d.index, d.target) for d in scan.dangling] == [
        ("MEMORY.md", "pages/gone.md")]
    assert scan.dangling[0].line > 0
    assert not scan.ok


def test_verify_passes_when_pages_links_resolve(tmp_path):
    store = _make_store(tmp_path / "store")
    _write_page(store, "kept", ["repo:a"])
    mi.compile_view(store, "a")

    scan = mr.Scan(os.path.join(store, "views", "a"))

    assert scan.dangling == []
