"""#78 — `okfmem store merge` / `okfmem store split` on label-layout stores."""
import json
import os
import subprocess
import sys

import pytest

import memory_init as mi
import memory_store as mst

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, val in (("GIT_AUTHOR_NAME", "tester"), ("GIT_AUTHOR_EMAIL", "t@e.st"),
                     ("GIT_COMMITTER_NAME", "tester"), ("GIT_COMMITTER_EMAIL", "t@e.st")):
        monkeypatch.setenv(key, val)


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _page(name, *repos, body="body"):
    labels = ", ".join(f"repo:{r}" for r in repos)
    return (f"---\nname: {name}\ndescription: {name} hook\ntype: project\n"
            f"labels: [{labels}]\n---\n\n{body}\n")


def _state(ts, body):
    return f"---\nname: state\nmodified: {ts}\n---\n\n{body}\n"


def _write(store, rel, text):
    path = store.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(store, rel):
    return store.joinpath(*rel.split("/")).read_text(encoding="utf-8")


def _commit_all(store, msg="seed"):
    _git(store, "add", "-A")
    _git(store, "commit", "-q", "-m", msg)


def make_store(path, files, registry=None):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _write(path, "layout.yml", "layout: labels\n")
    for rel, text in files.items():
        _write(path, rel, text)
    if registry is not None:
        _write(path, "registry.json", json.dumps(registry, indent=2) + "\n")
    _commit_all(path)
    return path


def snapshot(store):
    out = {}
    for root, dirs, files in os.walk(store):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, store)] = f.read()
    return out


def _merge(target, source, **kw):
    kw.setdefault("assume_yes", True)
    kw.setdefault("non_interactive", True)
    return mst.merge_stores(str(target), str(source), **kw)


def _split(source, projects, dest, **kw):
    kw.setdefault("assume_yes", True)
    kw.setdefault("non_interactive", True)
    return mst.split_store(str(source), projects, str(dest), **kw)


# --------------------------------------------------------------------------- merge


def test_merge_disjoint_imports_every_project_in_one_commit(tmp_path, capsys):
    source = make_store(tmp_path / "src", {
        "pages/alpha-note.md": _page("alpha-note", "alpha"),
        "pages/archive/2026/old.md": _page("old", "alpha"),
        "state/alpha.md": _state("2026-11-01T00:00:00Z", "alpha state"),
        "research/paper.md": "notes\n",
    })
    target = make_store(tmp_path / "dst", {
        "pages/beta-note.md": _page("beta-note", "beta"),
        "state/beta.md": _state("2026-11-01T00:00:00Z", "beta state"),
    })

    assert _merge(target, source) == 0

    for rel in ("pages/alpha-note.md", "pages/archive/2026/old.md", "state/alpha.md",
                "research/paper.md"):
        assert _read(target, rel) == _read(source, rel)
    assert "beta state" in _read(target, "state/beta.md")
    assert _git(target, "status", "--porcelain") == ""
    assert _git(target, "rev-list", "--count", "HEAD") == "2"
    assert "projects in source: alpha" in capsys.readouterr().out


def test_merge_overlapping_copies_new_pages_and_skips_identical(tmp_path):
    shared = _page("shared", "alpha")
    source = make_store(tmp_path / "src", {
        "pages/shared.md": shared,
        "pages/fresh.md": _page("fresh", "alpha"),
    })
    target = make_store(tmp_path / "dst", {"pages/shared.md": shared})

    assert _merge(target, source) == 0

    assert _read(target, "pages/fresh.md") == _read(source, "pages/fresh.md")
    assert not (target / "pages" / "shared.md.orphan").exists()


def test_merge_colliding_page_is_kept_as_orphan_never_overwritten(tmp_path):
    source = make_store(tmp_path / "src", {"pages/note.md": _page("note", "a", body="source")})
    target = make_store(tmp_path / "dst", {"pages/note.md": _page("note", "a", body="target")})

    assert _merge(target, source) == 0
    assert "target" in _read(target, "pages/note.md")
    assert "source" in _read(target, "pages/note.md.orphan")

    # Merging the same source again preserves nothing twice.
    assert _merge(target, source) == 0
    assert not (target / "pages" / "note.md.orphan.2").exists()

    # A different third version gets the next free suffix.
    _write(source, "pages/note.md", _page("note", "a", body="third"))
    _commit_all(source, "edit")
    assert _merge(target, source) == 0
    assert "third" in _read(target, "pages/note.md.orphan.2")
    assert "target" in _read(target, "pages/note.md")


def test_merge_state_is_last_write_wins(tmp_path):
    source = make_store(tmp_path / "src", {
        "state/alpha.md": _state("2026-11-02T10:00:00Z", "source alpha"),
        "state/beta.md": _state("2026-11-01T10:00:00Z", "source beta"),
    })
    target = make_store(tmp_path / "dst", {
        "state/alpha.md": _state("2026-11-01T10:00:00Z", "target alpha"),
        "state/beta.md": _state("2026-11-02T10:00:00Z", "target beta"),
    })

    assert _merge(target, source) == 0

    assert "source alpha" in _read(target, "state/alpha.md")
    assert "target beta" in _read(target, "state/beta.md")
    assert not (target / "state" / "alpha.md.orphan").exists()
    assert not (target / "state" / "beta.md.orphan").exists()


def test_merge_registry_keeps_both_machines_and_target_wins_conflicts(tmp_path, capsys):
    target = make_store(tmp_path / "dst", {}, registry={
        "version": 1, "layout": "labels",
        "map": {"/home/tester/code/x": "x", "/home/tester/code/clash": "mine"},
        "overrides": {}, "aliases": {"repo:x": ["xx"]},
    })
    source = make_store(tmp_path / "src", {}, registry={
        "version": 1,
        "map": {"/Users/you/code/y": "y", "/home/tester/code/clash": "theirs"},
        "overrides": {"/Users/you/code/y": {"org": "acme"}},
        "aliases": {"repo:y": ["yy"]},
        "stores": [{"name": "team", "path": "../team"}],
    })

    assert _merge(target, source) == 0

    reg = json.loads(_read(target, "registry.json"))
    assert "stores" not in reg
    assert reg["map"] == {"/home/tester/code/x": "x", "/home/tester/code/clash": "mine",
                          "/Users/you/code/y": "y"}
    assert reg["overrides"] == {"/Users/you/code/y": {"org": "acme"}}
    assert reg["aliases"] == {"repo:x": ["xx"], "repo:y": ["yy"]}
    assert reg["layout"] == "labels"
    assert "target kept on map: /home/tester/code/clash" in capsys.readouterr().out


def test_merge_unions_nested_org_and_stack_overrides(tmp_path, capsys):
    target = make_store(tmp_path / "dst", {}, registry={
        "map": {}, "overrides": {"org": {"x": "acme", "clash": "mine"},
                                 "stack": {"x": "py"}},
    })
    source = make_store(tmp_path / "src", {}, registry={
        "map": {}, "overrides": {"org": {"/Users/you/code/y": "globex", "clash": "theirs"},
                                 "stack": {"y": "go"}},
    })

    assert _merge(target, source) == 0

    reg = json.loads(_read(target, "registry.json"))
    assert reg["overrides"]["org"] == {"/Users/you/code/y": "globex", "clash": "mine",
                                       "x": "acme"}
    assert reg["overrides"]["stack"] == {"x": "py", "y": "go"}
    out = capsys.readouterr().out
    assert "+2 overrides" in out
    assert "target kept on overrides.org: clash" in out


def test_merge_skips_a_git_file_in_the_source(tmp_path):
    source = make_store(tmp_path / "src", {"pages/a.md": _page("a", "alpha")})
    worktree = tmp_path / "src-wt"
    _git(source, "worktree", "add", "-q", "--detach", str(worktree))
    assert (worktree / ".git").is_file()
    target = make_store(tmp_path / "dst", {})

    assert _merge(target, worktree) == 0

    assert (target / "pages" / "a.md").is_file()
    assert not (target / ".git.orphan").exists()
    assert ".git" not in mst.store_files(str(worktree))
    assert _git(target, "status", "--porcelain") == ""


def test_merge_refuses_a_malformed_source_registry(tmp_path):
    source = make_store(tmp_path / "src", {"registry.json": "{not json"})
    target = make_store(tmp_path / "dst", {})
    before = snapshot(target)
    assert _merge(target, source) == 1
    assert snapshot(target) == before


def test_merge_dry_run_writes_nothing(tmp_path, capsys):
    source = make_store(tmp_path / "src", {
        "pages/a.md": _page("a", "alpha"),
        "pages/note.md": _page("note", "alpha", body="source"),
    }, registry={"map": {"/Users/you/a": "alpha"}})
    target = make_store(tmp_path / "dst", {"pages/note.md": _page("note", "alpha", body="t")})
    before, head = snapshot(target), _git(target, "rev-parse", "HEAD")

    assert _merge(target, source, dry_run=True) == 0

    assert snapshot(target) == before
    assert _git(target, "rev-parse", "HEAD") == head
    out = capsys.readouterr().out
    assert "copy   pages/a.md" in out
    assert "note.md.orphan" in out


def test_merge_non_interactive_without_yes_writes_nothing(tmp_path, capsys):
    source = make_store(tmp_path / "src", {"pages/a.md": _page("a", "alpha")})
    target = make_store(tmp_path / "dst", {})
    before = snapshot(target)

    assert _merge(target, source, assume_yes=False) == 0

    assert snapshot(target) == before
    assert "okfmem store merge" in capsys.readouterr().out


def test_merge_refuses_a_dirty_target(tmp_path):
    source = make_store(tmp_path / "src", {"pages/a.md": _page("a", "alpha")})
    target = make_store(tmp_path / "dst", {})
    _write(target, "pages/wip.md", "uncommitted\n")
    assert _merge(target, source) == 1
    assert not (target / "pages" / "a.md").exists()


def test_merge_refuses_a_legacy_projects_store(tmp_path):
    source = tmp_path / "src"
    _write(source, "projects/alpha/note.md", "x\n")
    target = make_store(tmp_path / "dst", {})
    assert _merge(target, source) == 1


def test_merge_preserve_history_records_source_as_parent(tmp_path):
    source = make_store(tmp_path / "src", {"pages/a.md": _page("a", "alpha")})
    target = make_store(tmp_path / "dst", {"pages/b.md": _page("b", "beta")})
    source_head = _git(source, "rev-parse", "HEAD")

    assert _merge(target, source, preserve_history=True) == 0

    parents = _git(target, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert parents[2] == source_head
    assert (target / "pages" / "a.md").is_file()
    assert (target / "pages" / "b.md").is_file()
    assert _git(target, "status", "--porcelain") == ""


def test_store_merge_routes_through_the_dispatcher(tmp_path):
    source = make_store(tmp_path / "src", {"pages/a.md": _page("a", "alpha")})
    target = make_store(tmp_path / "dst", {})
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "okfmem"), "store", "merge", str(source),
         "--store", str(target), "--yes"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0, r.stderr
    assert (target / "pages" / "a.md").is_file()


# --------------------------------------------------------------------------- split


def _split_source(tmp_path):
    return make_store(tmp_path / "src", {
        "pages/alpha-one.md": _page("alpha-one", "alpha"),
        "pages/archive/2026/alpha-old.md": _page("alpha-old", "alpha"),
        "pages/both.md": _page("both", "alpha", "beta"),
        "pages/beta-one.md": _page("beta-one", "beta"),
        "state/alpha.md": _state("2026-11-01T00:00:00Z", "alpha state"),
        "state/beta.md": _state("2026-11-01T00:00:00Z", "beta state"),
    }, registry={
        "version": 1, "layout": "labels",
        "map": {"/home/tester/alpha": "alpha", "/Users/you/alpha": "alpha",
                "/home/tester/beta": "beta"},
        "overrides": {"/home/tester/alpha": {"org": "acme"}, "/home/tester/beta": {"org": "b"}},
        "aliases": {"repo:alpha": ["old-alpha"], "repo:beta": ["bb"]},
    })


ALPHA_FILES = ("pages/alpha-one.md", "pages/archive/2026/alpha-old.md", "pages/both.md",
               "state/alpha.md")


def test_split_scaffolds_an_initialized_store_and_leaves_the_source(tmp_path):
    source = _split_source(tmp_path)
    dest = tmp_path / "work-store"
    before, head = snapshot(source), _git(source, "rev-parse", "HEAD")

    assert _split(source, ["alpha"], dest) == 0

    assert mi.has_layout_marker(str(dest))
    assert mi.GITIGNORE_MARKER_OPEN in _read(dest, ".gitignore")
    assert _git(dest, "status", "--porcelain") == ""
    assert _git(dest, "rev-list", "--count", "HEAD") == "1"
    for rel in ALPHA_FILES:
        assert _read(dest, rel) == _read(source, rel)
    assert not (dest / "pages" / "beta-one.md").exists()
    assert not (dest / "state" / "beta.md").exists()
    reg = json.loads(_read(dest, "registry.json"))
    assert reg["map"] == {"/home/tester/alpha": "alpha", "/Users/you/alpha": "alpha"}
    assert reg["overrides"] == {"/home/tester/alpha": {"org": "acme"}}
    assert reg["aliases"] == {"repo:alpha": ["old-alpha"]}

    assert snapshot(source) == before
    assert _git(source, "rev-parse", "HEAD") == head


def test_split_into_an_existing_store_never_overwrites(tmp_path):
    source = _split_source(tmp_path)
    dest = make_store(tmp_path / "work", {"pages/alpha-one.md": _page("alpha-one", "alpha",
                                                                        body="dest copy")})
    assert _split(source, ["alpha"], dest) == 0
    assert "dest copy" in _read(dest, "pages/alpha-one.md")
    assert _read(dest, "pages/alpha-one.md.orphan") == _read(source, "pages/alpha-one.md")


def test_split_into_an_existing_store_unions_nested_org_overrides(tmp_path):
    source = make_store(tmp_path / "src", {"pages/a.md": _page("a", "alpha")}, registry={
        "map": {"/home/tester/alpha": "alpha"},
        "overrides": {"org": {"/home/tester/alpha": "acme", "beta": "b"}},
    })
    dest = make_store(tmp_path / "work", {}, registry={
        "map": {}, "overrides": {"org": {"gamma": "g"}},
    })

    assert _split(source, ["alpha"], dest) == 0

    reg = json.loads(_read(dest, "registry.json"))
    assert reg["overrides"]["org"] == {"/home/tester/alpha": "acme", "gamma": "g"}


def test_split_dry_run_writes_nothing(tmp_path, capsys):
    source = _split_source(tmp_path)
    dest = tmp_path / "work-store"
    before = snapshot(source)

    assert _split(source, ["alpha"], dest, delete_source=True, dry_run=True) == 0

    assert not dest.exists()
    assert snapshot(source) == before
    out = capsys.readouterr().out
    assert "delete pages/alpha-one.md" in out
    assert "keep   pages/both.md" in out


def test_split_delete_source_removes_projects_after_typed_confirmation(tmp_path, monkeypatch):
    source = _split_source(tmp_path)
    dest = tmp_path / "work-store"
    monkeypatch.setattr("builtins.input", lambda prompt="": "SPLIT")

    assert _split(source, ["alpha"], dest, delete_source=True, non_interactive=False,
                  assume_yes=False) == 0

    for rel in ALPHA_FILES:
        assert (dest / rel).is_file()
    for rel in ("pages/alpha-one.md", "pages/archive/2026/alpha-old.md", "state/alpha.md"):
        assert not (source / rel).exists()
    # A page another project still uses stays in the source.
    assert (source / "pages" / "both.md").is_file()
    assert (source / "pages" / "beta-one.md").is_file()
    assert _git(source, "status", "--porcelain") == ""
    assert _git(source, "rev-list", "--count", "HEAD") == "2"


def test_split_delete_source_wrong_word_changes_nothing(tmp_path, monkeypatch):
    source = _split_source(tmp_path)
    dest = tmp_path / "work-store"
    before = snapshot(source)
    monkeypatch.setattr("builtins.input", lambda prompt="": "split")

    assert _split(source, ["alpha"], dest, delete_source=True, non_interactive=False) == 1

    assert not dest.exists()
    assert snapshot(source) == before


def test_split_delete_source_is_not_covered_by_yes(tmp_path, capsys):
    source = _split_source(tmp_path)
    dest = tmp_path / "work-store"
    before = snapshot(source)

    assert _split(source, ["alpha"], dest, delete_source=True) == 0

    assert (dest / "pages" / "alpha-one.md").is_file()
    assert snapshot(source) == before
    assert "--delete-source" in capsys.readouterr().out


def test_split_delete_source_refuses_a_dirty_source(tmp_path, monkeypatch):
    source = _split_source(tmp_path)
    _write(source, "pages/wip.md", _page("wip", "alpha"))
    dest = tmp_path / "work-store"
    monkeypatch.setattr("builtins.input", lambda prompt="": "SPLIT")

    assert _split(source, ["alpha"], dest, delete_source=True, non_interactive=False) == 1

    assert not dest.exists()
    assert (source / "pages" / "alpha-one.md").is_file()


def test_split_unknown_project_fails(tmp_path):
    source = _split_source(tmp_path)
    dest = tmp_path / "work-store"
    assert _split(source, ["alpha", "nope"], dest) == 1
    assert not dest.exists()


def test_split_follows_registry_aliases(tmp_path):
    source = make_store(tmp_path / "src", {
        "pages/legacy.md": _page("legacy", "old-alpha"),
    }, registry={"map": {}, "overrides": {}, "aliases": {"repo:alpha": ["old-alpha"]}})
    dest = tmp_path / "work-store"
    assert _split(source, ["alpha"], dest) == 0
    assert (dest / "pages" / "legacy.md").is_file()
