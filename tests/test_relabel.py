"""Tests for `okfmem relabel` and init's rename hint (#238)."""

import json
import os
import shutil
import subprocess

import pytest

import memory_init as mi
import memory_relabel as mr

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)

OLD_URL = "https://github.com/old-org/gaal.git"
NEW_URL = "https://github.com/new-org/toran.git"


def _page(name, labels):
    return f"---\nname: {name}\nlabels: [{', '.join(labels)}]\n---\n\n# {name}\n"


def _store(tmp_path, root):
    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    (store / "state").mkdir()
    (store / "registry.json").write_text(
        json.dumps({"layout": "labels", "map": {root: "gaal"}, "overrides": {}}),
        encoding="utf-8",
    )
    (store / "pages" / "one.md").write_text(_page("one", ["repo:gaal"]), encoding="utf-8")
    (store / "pages" / "two.md").write_text(
        _page("two", ["repo:gaal", "stack:python"]), encoding="utf-8"
    )
    (store / "pages" / "other.md").write_text(_page("other", ["repo:elsewhere"]), encoding="utf-8")
    (store / "state" / "gaal.md").write_text(
        "---\nname: gaal-state\ntype: state\nmodified: 2026-11-01\n---\n\n# STATE — gaal\n\nWork.\n",
        encoding="utf-8",
    )
    return str(store)


def _moved_repo(tmp_path):
    root = tmp_path / "gaal"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", NEW_URL], check=True)
    return os.path.normpath(str(root))


def _tree(store):
    out = {}
    for base, _dirs, files in os.walk(store):
        if os.sep + ".git" in base:
            continue
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, store)] = open(p, "rb").read()
    return out


def test_hint_names_both_projects_when_new_is_empty_and_old_has_pages(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    reg = mi._load_registry(os.path.join(store, "registry.json"))
    old = mr.relabel_hint(store, str(tmp_path / "no-claude"), root, reg, "toran")
    assert old == "gaal"
    line = mr.format_hint(old, "toran")
    assert "gaal" in line and "toran" in line and "okfmem relabel gaal toran" in line


def test_no_hint_once_new_project_has_pages(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    assert mr.relabel(store, "gaal", "toran", out=lambda *_: None) == 0
    reg = mi._load_registry(os.path.join(store, "registry.json"))
    assert mr.relabel_hint(store, str(tmp_path / "x"), root, reg, "toran") is None


def test_no_hint_on_a_store_without_a_mismatch(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    reg = mi._load_registry(os.path.join(store, "registry.json"))
    assert mr.relabel_hint(store, str(tmp_path / "x"), root, reg, "gaal") is None
    assert mr.relabel_hint(store, str(tmp_path / "x"), root, reg, "fresh") == "gaal"
    reg["map"] = {}
    root2 = tmp_path / "unrelated"
    root2.mkdir()
    assert mr.relabel_hint(store, str(tmp_path / "x"), str(root2), reg, "fresh") is None


def test_relabel_adds_label_copies_state_and_compiles_view(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    assert mr.relabel(store, "gaal", "toran", out=lambda *_: None) == 0
    one = open(os.path.join(store, "pages", "one.md"), encoding="utf-8").read()
    assert "labels: [repo:gaal, repo:toran]" in one
    two = open(os.path.join(store, "pages", "two.md"), encoding="utf-8").read()
    assert "labels: [repo:gaal, stack:python, repo:toran]" in two
    other = open(os.path.join(store, "pages", "other.md"), encoding="utf-8").read()
    assert "repo:toran" not in other
    assert os.path.isfile(os.path.join(store, "state", "toran.md"))
    assert "Work." in open(os.path.join(store, "state", "toran.md"), encoding="utf-8").read()
    view = open(os.path.join(store, "views", "toran", "MEMORY.md"), encoding="utf-8").read()
    assert "pages/one.md" in view and "pages/two.md" in view and "other.md" not in view


def test_relabel_run_twice_changes_nothing_the_second_time(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    assert mr.relabel(store, "gaal", "toran", out=lambda *_: None) == 0
    before = _tree(store)
    lines = []
    assert mr.relabel(store, "gaal", "toran", out=lines.append) == 0
    assert _tree(store) == before
    assert lines[0].startswith("added repo:toran to 0 of 2")


def test_relabel_keeps_a_real_new_state(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    new_state = os.path.join(store, "state", "toran.md")
    with open(new_state, "w", encoding="utf-8") as f:
        f.write("---\nname: toran-state\n---\n\n# STATE — toran\n\nReal notes.\n")
    assert mr.relabel(store, "gaal", "toran", out=lambda *_: None) == 0
    assert "Real notes." in open(new_state, encoding="utf-8").read()


def test_relabel_dry_run_writes_nothing(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    before = _tree(store)
    assert mr.relabel(store, "gaal", "toran", dry_run=True, out=lambda *_: None) == 0
    assert _tree(store) == before


def test_relabel_refuses_unknown_old_and_bad_names(tmp_path):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    assert mr.relabel(store, "nope", "toran", out=lambda *_: None) == 1
    assert mr.relabel(store, "gaal", "../x", out=lambda *_: None) == 2
    assert mr.relabel(store, "gaal", "gaal", out=lambda *_: None) == 2


def test_init_prints_the_hint_once_and_not_after_relabel(tmp_path, monkeypatch, capsys):
    root = _moved_repo(tmp_path)
    store = _store(tmp_path, root)
    reg_path = os.path.join(store, "registry.json")
    reg = mi._load_registry(reg_path)
    reg["map"][root] = "toran"
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(reg, f)
    monkeypatch.chdir(root)
    mi.cmd_run(store, True, False)
    out = capsys.readouterr().out
    assert out.count("okfmem relabel gaal toran") == 1
    assert mr.relabel(store, "gaal", "toran", out=lambda *_: None) == 0
    mi.cmd_run(store, True, False)
    assert "okfmem relabel" not in capsys.readouterr().out
