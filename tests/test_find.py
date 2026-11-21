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


def by_match(results, kind):
    return [r for r in results if r["match"] == kind]


def test_page_linked_from_a_match_is_related_with_via(store):
    page(store / "pages", "hit", body="watch [fix](answer.md)\n")
    page(store / "pages", "answer", body="unrelated words\n")
    res = mf.find(str(store), ["watch"])
    assert slugs(by_match(res, "terms")) == ["hit"]
    [rel] = by_match(res, "link")
    assert (rel["slug"], rel["via"]) == ("answer", ["hit"])


def test_page_linking_to_a_match_is_related(store):
    page(store / "pages", "hit", body="watch\n")
    page(store / "pages", "pointer", body="see [it](./hit.md#top)\n")
    [rel] = by_match(mf.find(str(store), ["watch"]), "link")
    assert (rel["slug"], rel["via"]) == ("pointer", ["hit"])


def test_direct_match_is_never_related_and_limit_keeps_direct(store):
    page(store / "pages", "a", body="watch [b](b.md) [c](c.md)\n", last="2026-02-01")
    page(store / "pages", "b", body="watch\n", last="2026-01-01")
    page(store / "pages", "c", body="nothing\n")
    res = mf.find(str(store), ["watch"], limit=1)
    assert slugs(by_match(res, "terms")) == ["b"]
    assert slugs(by_match(res, "link")) == []
    full = mf.find(str(store), ["watch"], limit=0)
    assert sorted(slugs(by_match(full, "terms"))) == ["a", "b"]
    assert slugs(by_match(full, "link")) == ["c"]


def test_related_zero_is_todays_output_and_n_caps_it(store):
    links = " ".join(f"[x](r{i}.md)" for i in range(5))
    page(store / "pages", "hit", body=f"watch {links}\n")
    for i in range(5):
        page(store / "pages", f"r{i}", body="none\n")
    off = mf.find(str(store), ["watch"], related=0)
    assert slugs(off) == ["hit"] and all(r["match"] == "terms" for r in off)
    assert len(by_match(mf.find(str(store), ["watch"]), "link")) == 3
    assert len(by_match(mf.find(str(store), ["watch"], related=2), "link")) == 2


def test_hub_boost_breaks_ties_but_name_tier_wins(store):
    page(store / "pages", "plain", body="watch\n", last="2026-06-01")
    page(store / "pages", "hub", body="watch\n", last="2026-01-01")
    page(store / "pages", "p1", body="watch [h](hub.md)\n")
    page(store / "pages", "p2", body="watch [h](hub.md)\n")
    page(store / "pages", "named", name="Watch", body="x\n")
    order = slugs(by_match(mf.find(str(store), ["watch"], limit=0), "terms"))
    assert order[0] == "named"
    assert order.index("hub") < order.index("plain")


def test_links_to_urls_missing_and_archived_pages_are_ignored(store):
    page(
        store / "pages",
        "hit",
        body="watch [u](https://x.test/y.md) [m](nope.md) [a](archive/old.md) [up](../etc.md)\n",
    )
    page(store / "pages" / "archive", "old", body="stale\n")
    assert by_match(mf.find(str(store), ["watch"]), "link") == []
    res = mf.find(str(store), ["watch"], archive=True)
    assert [r["path"] for r in by_match(res, "link")] == ["pages/archive/old.md"]


def test_repo_filter_applies_to_related_pages(store):
    page(store / "pages", "hit", labels=["repo:gaal"], body="watch [a](in.md) [b](out.md)\n")
    page(store / "pages", "in", labels=["repo:gaal"], body="x\n")
    page(store / "pages", "out", body="x\n")
    res = mf.find(str(store), ["watch"], repo="gaal")
    assert slugs(by_match(res, "link")) == ["in"]


def test_json_carries_match_and_via(store, capsys):
    page(store / "pages", "hit", body="watch [a](ans.md)\n")
    page(store / "pages", "ans", body="x\n")
    assert mf.main(["watch", "--json", "--store", str(store)]) == 0
    direct, rel = json.loads(capsys.readouterr().out)
    assert direct["match"] == "terms" and "via" not in direct
    assert (rel["match"], rel["via"]) == ("link", ["hit"])


def test_text_output_lists_related_under_a_heading(store, capsys):
    page(store / "pages", "hit", body="watch [a](ans.md)\n")
    page(store / "pages", "ans", body="x\n")
    assert mf.main(["watch", "--store", str(store)]) == 0
    out = capsys.readouterr().out
    assert out.index("pages/hit.md") < out.index("related:") < out.index("pages/ans.md")
    assert "via hit" in out
    assert mf.main(["watch", "--related", "-1", "--store", str(store)]) == 2


def test_no_direct_match_means_no_related_and_exit_one(store):
    page(store / "pages", "a", body="[b](b.md)\n")
    page(store / "pages", "b", body="x\n")
    assert mf.find(str(store), ["absent"]) == []
    assert mf.main(["absent", "--store", str(store)]) == 1


def test_find_reads_each_page_file_once(store, monkeypatch):
    page(store / "pages", "hit", body="watch [a](ans.md)\n")
    page(store / "pages", "ans", body="x\n")
    reads = []
    real = mf.read_page
    monkeypatch.setattr(mf, "read_page", lambda p: reads.append(p) or real(p))
    mf.find(str(store), ["watch"])
    assert len(reads) == len(set(reads)) == 2
