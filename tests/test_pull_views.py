"""`okfmem pull` recompiles the views a pull changed and prunes copied pages (#249)."""
import json
import os
import shutil
from pathlib import Path

import pytest

import memory_init as mi
import memory_pull as mp
from test_pull import _advance_remote, _bare, _clone, _commit, _git
from test_views_projection import _write_page

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")

PAGE = ("---\nname: {n}\ndescription: d {n}\nimportance: 1\n"
        "last_accessed: 2026-10-01\nlabels: [{labels}]\n---\n\n# {n}\n")


def _page(slug, labels):
    return PAGE.format(n=slug, labels=labels)


def _seed_store(tmp_path):
    origin = _bare(tmp_path)
    store = _clone(origin, str(tmp_path / "store"))
    _commit(store, ".layout", "layout: labels\n")
    _commit(store, ".gitignore", "views/\n")
    _commit(store, "registry.json",
            json.dumps({"map": {}, "overrides": {}, "aliases": {}}))
    _commit(store, "pages/first.md", _page("first", "repo:x"))
    _git(store, "push", "-q", "-u", "origin", "HEAD")
    mi.compile_view(store, "x")
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "state")
    _git(store, "push", "-q")
    return origin, store


def _view(store, name):
    return Path(store, "views", name, "MEMORY.md").read_text(encoding="utf-8")


def _head(store):
    return _git(store, "rev-parse", "HEAD").stdout.strip()


def _run_pull(store, monkeypatch, *extra):
    monkeypatch.setattr("sys.argv", ["pull", "--store", store, "--quiet", *extra])
    monkeypatch.setattr("sys.stdin", None)
    with pytest.raises(SystemExit) as exc:
        mp.main()
    assert exc.value.code == 0


def test_pull_lists_a_newly_labelled_page_in_the_view(tmp_path, monkeypatch):
    origin, store = _seed_store(tmp_path)
    _advance_remote(tmp_path, origin, "new", _page("second", "repo:x"),
                    fname="pages/second.md")
    before = _head(store)

    _run_pull(store, monkeypatch)

    assert "pages/second.md" in _view(store, "x")
    assert _head(store) != before
    assert _git(store, "status", "--porcelain").stdout == ""
    assert _git(store, "rev-list", "--count", "@{u}..HEAD").stdout.strip() == "0"


def test_pull_that_changes_nothing_recompiles_no_view(tmp_path, monkeypatch):
    _, store = _seed_store(tmp_path)
    calls = []
    monkeypatch.setattr("memory_consolidate._refresh_all_views",
                        lambda path, skip=None: calls.append(path))

    _run_pull(store, monkeypatch)

    assert calls == []


def test_no_views_flag_fetches_without_touching_views(tmp_path, monkeypatch):
    origin, store = _seed_store(tmp_path)
    _advance_remote(tmp_path, origin, "new", _page("second", "repo:x"),
                    fname="pages/second.md")
    before = _head(store)

    _run_pull(store, monkeypatch, "--no-views")

    assert _head(store) != before
    assert "pages/second.md" not in _view(store, "x")


def test_attached_store_pull_refreshes_home_views(tmp_path, monkeypatch):
    origin, store = _seed_store(tmp_path)
    att_origin = _bare(tmp_path, "att.git")
    att = _clone(att_origin, str(tmp_path / "att"))
    _commit(att, "pages/seed.md", _page("seed", "repo:other"))
    _git(att, "push", "-q", "-u", "origin", "HEAD")
    reg = {"map": {}, "overrides": {}, "aliases": {},
           "stores": [{"name": "att", "path": att}]}
    Path(store, "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    _git(store, "commit", "-qam", "attach")
    _git(store, "push", "-q")
    _advance_remote(tmp_path, att_origin, "att", _page("shared", "repo:x"),
                    fname="pages/shared.md")

    _run_pull(store, monkeypatch)

    assert "stores/att/shared.md" in _view(store, "x")


def _copy_pages_into_view(store, name):
    pages = Path(store, "views", name, "pages")
    if pages.is_symlink():
        pages.unlink()
    elif pages.exists() and mi._is_junction(str(pages)):
        os.rmdir(pages)
    shutil.copytree(Path(store, "pages"), pages)
    return pages


def test_relabelled_page_leaves_a_copied_view_pages_dir(tmp_path):
    store = str(tmp_path / "store")
    _write_page(store, "kept", ["repo:x"])
    _write_page(store, "moved", ["repo:x"])
    Path(store, ".layout").write_text("layout: labels\n", encoding="utf-8")
    mi.compile_view(store, "x")
    pages = _copy_pages_into_view(store, "x")
    assert (pages / "moved.md").exists()
    _write_page(store, "moved", ["repo:y"])

    mi.compile_view(store, "x")

    assert (pages / "kept.md").exists()
    assert not (pages / "moved.md").exists()
    assert Path(store, "pages", "moved.md").exists()


def test_linked_view_pages_dir_is_never_pruned(tmp_path):
    store = str(tmp_path / "store")
    _write_page(store, "other", ["repo:y"])
    _write_page(store, "kept", ["repo:x"])
    Path(store, ".layout").write_text("layout: labels\n", encoding="utf-8")

    mi.compile_view(store, "x")

    assert Path(store, "pages", "other.md").exists()
    assert Path(store, "pages", "kept.md").exists()


def test_copied_view_pages_dir_gets_a_new_page(tmp_path):
    store = str(tmp_path / "store")
    _write_page(store, "kept", ["repo:x"])
    Path(store, ".layout").write_text("layout: labels\n", encoding="utf-8")
    mi.compile_view(store, "x")
    pages = _copy_pages_into_view(store, "x")
    _write_page(store, "fresh", ["repo:x"])

    mi.compile_view(store, "x")

    assert (pages / "fresh.md").exists()
