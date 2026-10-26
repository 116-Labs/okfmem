"""Consolidation over the flat `pages/` pool (#101).

Driven through the module's `main()`, in a subprocess where the wiring is what
is under test. `OKFMEM_NO_STATUS=1` keeps the badge writer off the real home
dir; `--no-commit` keeps git out of it. Fixtures are synthetic: every page is
created and last read on 2026-01-01, so by 2026-07-31 a transient page is far
past every archive gate.
"""
import os
import subprocess
import sys
from datetime import date

import memory_consolidate as mc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = "2026-07-31"


def _page(name, extra=""):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {name} page\n"
        "type: project\n"
        "created: 2026-01-01\n"
        "last_accessed: 2026-01-01\n"
        "access_count: 0\n"
        f"{extra}"
        "---\n\n"
        f"# {name}\n\nBody.\n"
    )


def _store(tmp_path, pages, migrated=True):
    store = tmp_path / "store"
    pdir = store / "pages"
    pdir.mkdir(parents=True)
    for slug, extra in pages.items():
        (pdir / f"{slug}.md").write_text(_page(slug, extra), encoding="utf-8")
    (store / "decay_state.json").write_text(
        '{"epoch": "2026-01-01"}\n', encoding="utf-8")
    if migrated:
        (store / "layout.yml").write_text("layout: labels\n", encoding="utf-8")
    return store


def _consolidate(store, *extra, check=True):
    env = dict(os.environ, OKFMEM_NO_STATUS="1")
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "memory_consolidate.py"),
         "--store", str(store), "--no-commit", "--today", TODAY, *extra],
        capture_output=True, text=True, env=env, cwd=ROOT, check=check)


def _snapshot(root):
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                snap[p] = f.read()
    return snap


def _archived(store, slug):
    return store / "pages" / "archive" / "2026" / f"{slug}.md"


def test_evergreen_page_is_never_archived_and_does_not_consume_the_cap(tmp_path):
    store = _store(tmp_path, {"a-evergreen": "scope: evergreen\n",
                              "b-plain": ""})

    out = _consolidate(store, "--cap", "1").stdout

    assert "archive candidates: 1 (archiving 1, cap 1)" in out
    assert (store / "pages" / "a-evergreen.md").is_file()
    assert _archived(store, "b-plain").is_file()


def test_org_label_alone_does_not_exempt_a_page(tmp_path):
    store = _store(tmp_path, {"org-only": "labels: [org:116-ideas]\n"})

    _consolidate(store)

    assert not (store / "pages" / "org-only.md").exists()
    assert _archived(store, "org-only").is_file()


def test_pinned_user_and_feedback_exemptions_hold_beside_evergreen(tmp_path):
    store = _store(tmp_path, {
        "pinned": "pinned: true\n",
        "evergreen": "scope: evergreen\n",
        "plain": "",
    })
    for typ in ("user", "feedback"):
        (store / "pages" / f"{typ}.md").write_text(
            _page(typ).replace("type: project", f"type: {typ}"), encoding="utf-8")

    out = _consolidate(store).stdout

    assert "archive candidates: 1 " in out
    for slug in ("pinned", "evergreen", "user", "feedback"):
        assert (store / "pages" / f"{slug}.md").is_file(), slug
    assert _archived(store, "plain").is_file()


def test_multi_repo_page_touched_by_any_repo_session_is_kept(tmp_path):
    store = _store(tmp_path, {"shared": "labels: [repo:alpha, repo:beta]\n"})
    page = store / "pages" / "shared.md"
    transcript = tmp_path / "beta-session.jsonl"
    transcript.write_text(f"read {page}\n", encoding="utf-8")

    _consolidate(store, "--transcript", str(transcript))

    text = page.read_text(encoding="utf-8")
    assert f"last_accessed: {TODAY}" in text
    assert "access_count: 1" in text
    assert "labels: [repo:alpha, repo:beta]" in text


def test_multi_repo_page_untouched_by_every_repo_is_archived(tmp_path):
    store = _store(tmp_path, {"shared": "labels: [repo:alpha, repo:beta]\n"})

    _consolidate(store)

    assert _archived(store, "shared").is_file()


def test_page_touched_through_a_memory_link_resolving_into_pages(
        tmp_path, monkeypatch):
    store = _store(tmp_path, {"linked": ""})
    pages = store / "pages"
    alias = tmp_path / "claude" / "projects" / "enc" / "memory"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(f"read {os.path.join(str(alias), 'linked.md')}\n",
                          encoding="utf-8")
    monkeypatch.setenv("OKFMEM_NO_STATUS", "1")
    monkeypatch.setattr(mc, "build_alias_index", lambda _store: {
        os.path.realpath(str(pages)): [str(alias)]})
    monkeypatch.setattr(sys, "argv", [
        "memory_consolidate.py", "--store", str(store), "--no-commit",
        "--today", TODAY, "--transcript", str(transcript)])

    mc.main()

    assert "access_count: 1" in (pages / "linked.md").read_text(encoding="utf-8")


def test_state_index_and_archived_files_are_never_candidates(tmp_path):
    store = _store(tmp_path, {})
    (store / "state").mkdir()
    (store / "state" / "demo.md").write_text(_page("demo-state"), encoding="utf-8")
    for name in ("MEMORY.md", "MEMORY-lane.md"):
        (store / "pages" / name).write_text(_page(name[:-3]), encoding="utf-8")
    old = store / "pages" / "archive" / "2025" / "old.md"
    old.parent.mkdir(parents=True)
    old.write_text(_page("old"), encoding="utf-8")
    before = _snapshot(str(store))

    out = _consolidate(store).stdout

    assert "archive candidates: 0 " in out
    # The end-of-run view refresh (#105) may add files for the cwd's repo;
    # every file that existed before must be untouched.
    after = _snapshot(str(store))
    assert {p: after.get(p) for p in before} == before


def test_archive_goes_to_the_archived_on_year_and_keeps_labels(tmp_path):
    store = _store(tmp_path, {"aged": "labels: [repo:alpha, stack:python]\n"})

    _consolidate(store)

    dest = _archived(store, "aged")
    text = dest.read_text(encoding="utf-8")
    assert "status: archived" in text
    assert f"archived_on: {TODAY}" in text
    assert "labels: [repo:alpha, stack:python]" in text
    assert not (store / "pages" / "aged.md").exists()


def test_same_slug_already_archived_that_year_gets_a_suffix(tmp_path):
    store = _store(tmp_path, {"aged": ""})
    existing = _archived(store, "aged")
    existing.parent.mkdir(parents=True)
    existing.write_text("earlier archive\n", encoding="utf-8")

    _consolidate(store)

    assert existing.read_text(encoding="utf-8") == "earlier archive\n"
    assert "status: archived" in _archived(store, "aged-2").read_text(
        encoding="utf-8")


def test_archive_dest_counts_up_past_every_taken_suffix(tmp_path):
    year_dir = tmp_path / "pages" / "archive" / "2026"
    year_dir.mkdir(parents=True)
    for name in ("x.md", "x-2.md"):
        (year_dir / name).write_text("taken\n", encoding="utf-8")

    assert mc.archive_dest(str(tmp_path), "x", 2026) == str(year_dir / "x-3.md")


def test_archive_page_retries_when_destination_is_claimed_concurrently(
        tmp_path, monkeypatch):
    store = _store(tmp_path, {})
    pages = store / "pages"
    first = pages / "first" / "same.md"
    second = pages / "second" / "same.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(_page("first"), encoding="utf-8")
    second.write_text(_page("second"), encoding="utf-8")

    choose_destination = mc.archive_dest
    contested = choose_destination(str(store), "same", 2026)
    calls = 0

    def collide_once(store_path, slug, year):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return contested
        return choose_destination(store_path, slug, year)

    monkeypatch.setattr(mc, "archive_dest", collide_once)
    today = date(2026, 7, 31)
    first_dest = mc.archive_page(
        {"path": str(first), "text": first.read_text(encoding="utf-8")},
        today, dry_run=False, store=str(store))
    second_dest = mc.archive_page(
        {"path": str(second), "text": second.read_text(encoding="utf-8")},
        today, dry_run=False, store=str(store))

    assert first_dest != second_dest
    assert "name: first" in open(first_dest, encoding="utf-8").read()
    assert "name: second" in open(second_dest, encoding="utf-8").read()
    assert not first.exists()
    assert not second.exists()


def test_unmigrated_store_is_refused_with_non_blocking_exit_1_and_left_untouched(
        tmp_path):
    store = _store(tmp_path, {"aged": ""}, migrated=False)
    legacy = store / "projects" / "demo"
    legacy.mkdir(parents=True)
    (legacy / "old.md").write_text(_page("old"), encoding="utf-8")
    before = _snapshot(str(store))

    res = _consolidate(store, check=False)

    assert res.returncode == 1
    assert "okfmem migrate-to-labels" in res.stderr
    assert _snapshot(str(store)) == before


def test_migrated_store_with_leftover_projects_dir_runs(tmp_path):
    store = _store(tmp_path, {"aged": ""})
    (store / "projects" / "demo").mkdir(parents=True)

    _consolidate(store)

    assert _archived(store, "aged").is_file()


def test_store_without_pages_dir_is_refused_with_non_blocking_exit_1(tmp_path):
    store = tmp_path / "store"
    store.mkdir()

    res = _consolidate(store, check=False)

    assert res.returncode == 1
    assert "no pages/" in res.stderr
