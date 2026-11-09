"""Tests for `okfmem find`: ranked page search that does not go through MEMORY.md.

All fixtures are synthetic — no real store, no home paths (leak gate).
"""

import json
import os
import subprocess
import sys

import pytest

import memory_find as mf
import memory_init as mi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page(d, slug, name=None, desc="", labels=(), body="body\n", last="2026-01-01"):
    fm = [f"name: {name or slug}"]
    if desc:
        fm.append(f"description: {desc}")
    if labels:
        fm.append(f"labels: [{', '.join(labels)}]")
    fm.append(f"last_accessed: {last}")
    (d / f"{slug}.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8"
    )


@pytest.fixture
def store(tmp_path):
    (tmp_path / "pages" / "archive").mkdir(parents=True)
    return tmp_path


def slugs(results):
    return [r["slug"] for r in results]


def test_name_hit_outranks_body_hit(store):
    page(store / "pages", "body-only", body="keychain keychain\n")
    page(store / "pages", "named", name="Keychain exit 36")
    assert slugs(mf.find(str(store), ["keychain"])) == ["named", "body-only"]


def test_every_term_must_match(store):
    page(store / "pages", "both", body="pool idle\n")
    page(store / "pages", "one", body="pool busy\n")
    assert slugs(mf.find(str(store), ["pool", "idle"])) == ["both"]


def test_match_is_case_insensitive_and_reads_the_slug(store):
    page(store / "pages", "idle-pool-diagnosis", name="x")
    assert slugs(mf.find(str(store), ["IDLE", "Pool"])) == ["idle-pool-diagnosis"]


def test_repo_filter_keeps_only_that_label(store):
    page(store / "pages", "a", labels=["repo:gaal"], body="watch\n")
    page(store / "pages", "b", labels=["repo:hive"], body="watch\n")
    assert slugs(mf.find(str(store), ["watch"], repo="GAAL")) == ["a"]


def test_archive_is_skipped_unless_asked(store):
    page(store / "pages" / "archive", "old", body="watch\n")
    assert mf.find(str(store), ["watch"]) == []
    assert [r["path"] for r in mf.find(str(store), ["watch"], archive=True)] == [
        "pages/archive/old.md"
    ]


def test_ties_break_on_most_recent_access(store):
    page(store / "pages", "older", body="watch\n", last="2026-01-01")
    page(store / "pages", "newer", body="watch\n", last="2026-02-01")
    assert slugs(mf.find(str(store), ["watch"])) == ["newer", "older"]


def test_index_and_state_files_are_not_results(store):
    (store / "pages" / "MEMORY.md").write_text("watch\n", encoding="utf-8")
    page(store / "pages", "real", body="watch\n")
    assert slugs(mf.find(str(store), ["watch"])) == ["real"]


def test_limit_caps_results(store):
    for i in range(5):
        page(store / "pages", f"p{i}", body="watch\n")
    assert len(mf.find(str(store), ["watch"], limit=2)) == 2


def test_snippet_is_first_matching_body_line(store):
    page(store / "pages", "p", body="intro\nthe watch stalls here\n")
    assert mf.find(str(store), ["watch"])[0]["snippet"] == "the watch stalls here"


def test_cli_exit_codes(store, capsys):
    page(store / "pages", "p", body="watch\n")
    assert mf.main(["watch", "--store", str(store)]) == 0
    assert "pages/p.md" in capsys.readouterr().out
    assert mf.main(["absent", "--store", str(store)]) == 1
    assert "--archive" in capsys.readouterr().err
    assert mf.main(["--store", str(store)]) == 2


def test_cli_without_pages_dir_is_a_usage_error(tmp_path, capsys):
    assert mf.main(["watch", "--store", str(tmp_path)]) == 2
    assert "no pages/" in capsys.readouterr().err


def test_cli_json_output(store, capsys):
    page(store / "pages", "p", desc="d", labels=["repo:gaal"], body="watch\n")
    assert mf.main(["watch", "--json", "--store", str(store)]) == 0
    [r] = json.loads(capsys.readouterr().out)
    assert (r["slug"], r["labels"]) == ("p", ["repo:gaal"])


def test_name_hit_outranks_many_body_hits(store):
    page(store / "pages", "body-heavy", body="watch " * 9 + "\n", last="2026-12-01")
    page(store / "pages", "named", name="Watch stalls", last="2026-01-01")
    assert slugs(mf.find(str(store), ["watch"], limit=1)) == ["named"]


def test_name_hit_outranks_description_plus_labels(store):
    page(store / "pages", "both", desc="gaal notes", labels=["repo:gaal"])
    page(store / "pages", "named", name="gaal")
    assert slugs(mf.find(str(store), ["gaal"])) == ["named", "both"]


def test_page_without_frontmatter_is_searchable(store):
    (store / "pages" / "plain.md").write_text(
        "# Plain\n\nthe watch log\n", encoding="utf-8"
    )
    [r] = mf.find(str(store), ["watch"])
    assert (r["slug"], r["snippet"]) == ("plain", "the watch log")


def test_cli_rejects_blank_query_and_negative_limit(store, capsys):
    assert mf.main(["", " ", "--store", str(store)]) == 2
    assert "search term" in capsys.readouterr().err
    assert mf.main(["watch", "--limit", "-1", "--store", str(store)]) == 2
    assert "--limit" in capsys.readouterr().err


def test_limit_zero_lists_every_match(store):
    for i in range(12):
        page(store / "pages", f"p{i}", body="watch\n")
    assert len(mf.find(str(store), ["watch"], limit=0)) == 12


def test_name_hit_outranks_description_hits_across_terms(store):
    page(store / "pages", "log-notes", name="Watch log", body="stall\n")
    page(store / "pages", "triage", desc="watch stall triage", body="watch stall\n")
    assert slugs(mf.find(str(store), ["watch", "stall"])) == ["log-notes", "triage"]


def test_hyphenated_slug_query_matches_the_slug(store):
    page(store / "pages", "idle-pool-diagnosis", name="Idle pool diagnosis")
    [r] = mf.find(str(store), ["idle-pool-diagnosis"])
    assert (r["slug"], r["tier_hits"]["name"]) == ("idle-pool-diagnosis", 1)


def test_both_pointer_blocks_tell_agents_to_run_find(tmp_path):
    with_store, fallback = tmp_path / "a.md", tmp_path / "b.md"
    mi.upsert_pointer(str(with_store), False, store=str(tmp_path / "store"))
    mi.upsert_pointer(str(fallback), False)
    assert "okfmem find" in with_store.read_text(encoding="utf-8")
    text = fallback.read_text(encoding="utf-8")
    assert "okfmem find" in text and "projects/" not in text


def test_dispatcher_routes_find(store):
    page(store / "pages", "p", body="watch\n")
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "okfmem"), "find", "watch",
         "--store", str(store)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "pages/p.md" in r.stdout
