"""Coverage for the shared frontmatter module (#98): `labels` / `scope`
parsing, the merging writer, and the read-only summary line."""
import memory_backfill
import memory_consolidate
import memory_frontmatter as fmod
import memory_reindex


def page(*lines, eol="\n"):
    return eol.join(["---", *lines, "---", "", "body", ""])


def labels(text):
    return fmod.parse_labels(text, warn=None)


def test_inline_labels():
    assert labels(page("labels: [org:116-ideas, Stack:TypeScript ]")) == [
        "org:116-ideas", "stack:typescript"]


def test_block_labels():
    text = page("name: x", "labels:", "  - repo:toran", "  - repo:foo.js", "type: project")
    assert labels(text) == ["repo:toran", "repo:foo.js"]


def test_block_labels_unindented_and_quoted():
    assert labels(page("labels:", "- 'a'", '- "b"')) == ["a", "b"]


def test_empty_and_absent_labels():
    assert labels(page("labels: []")) == []
    assert labels(page("labels:")) == []
    assert labels(page("name: x")) == []
    assert labels("no frontmatter here") == []


def test_malformed_labels_warn_and_return_empty(capsys):
    assert fmod.parse_labels(page("labels: [a, b")) == []
    assert fmod.parse_labels(page("labels: just-a-scalar"), source="p.md") == []
    err = capsys.readouterr().err
    assert err.count("malformed `labels` field") == 2
    assert "in p.md" in err


def test_invalid_labels_dropped_with_warning(capsys):
    text = page("labels: [good, 'Bad Label', a:b:c, UPPER, '']")
    assert fmod.parse_labels(text) == ["good", "upper"]
    err = capsys.readouterr().err
    assert "'Bad Label'" in err and "'a:b:c'" in err


def test_duplicate_labels_deduped():
    assert labels(page("labels: [a, A, b, a]")) == ["a", "b"]


def test_crlf_parsing():
    text = page("labels:", "  - a", "  - b", "scope: evergreen", eol="\r\n")
    assert labels(text) == ["a", "b"]
    assert fmod.parse_scope(text, warn=None) == "evergreen"


def test_scope_defaults_and_unknown(capsys):
    assert fmod.parse_scope(page("name: x")) == "transient"
    assert fmod.parse_scope("plain text") == "transient"
    assert fmod.parse_scope(page("scope:")) == "transient"
    assert fmod.parse_scope(page("scope: Evergreen")) == "evergreen"
    assert capsys.readouterr().err == ""
    assert fmod.parse_scope(page("scope: forever"), source="p.md") == "transient"
    assert "unknown scope 'forever' in p.md" in capsys.readouterr().err


def test_merge_appends_and_dedupes_inline():
    text = page("name: x", "labels: [a]", "type: project")
    out = fmod.merge_labels(text, ["B", "a", "c"])
    assert out == page("name: x", "labels: [a, b, c]", "type: project")


def test_merge_converts_block_to_inline_leaving_rest_identical():
    text = page("name: x", "labels:", "  - a", "  - b", "type: project")
    out = fmod.merge_labels(text, ["c"])
    assert out == page("name: x", "labels: [a, b, c]", "type: project")


def test_merge_adds_field_before_closing_delimiter():
    text = page("name: x", "type: project")
    assert fmod.merge_labels(text, ["a"]) == page("name: x", "type: project", "labels: [a]")


def test_merge_noop_is_byte_identical():
    text = page("name: x", "labels: [a, b]", "type: project")
    assert fmod.merge_labels(text, ["a"]) == text
    assert fmod.merge_labels(page("name: x"), []) == page("name: x")


def test_merge_preserves_crlf_round_trip():
    text = page("name: x", "labels:", "  - a", "type: project", eol="\r\n")
    out = fmod.merge_labels(text, ["b"])
    assert out == page("name: x", "labels: [a, b]", "type: project", eol="\r\n")
    assert labels(out) == ["a", "b"]


def test_merge_drops_invalid_new_label(capsys):
    out = fmod.merge_labels(page("name: x"), ["no good"])
    assert out == page("name: x")
    assert "dropping invalid label" in capsys.readouterr().err


def test_merge_without_frontmatter_raises():
    try:
        fmod.merge_labels("body only", ["a"])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_backfill_and_consolidate_share_the_parser():
    assert memory_backfill.find_frontmatter is fmod.find_frontmatter
    assert memory_consolidate.find_frontmatter is fmod.find_frontmatter
    assert memory_consolidate.parse_fields is fmod.parse_fields


def test_summary_counts_and_format(tmp_path):
    (tmp_path / "a.md").write_text(
        page("labels: [repo:x, org:o, stack:s, topic:t, misc]", "scope: evergreen"))
    (tmp_path / "b.md").write_text(page("name: plain"))
    (tmp_path / "c.md").write_text("no frontmatter")
    summary = fmod.summarize_pages(
        [str(tmp_path / n) for n in ("a.md", "b.md", "c.md", "missing.md")])
    assert summary == {
        "labeled": 1,
        "namespaces": {"repo": 1, "org": 1, "stack": 1, "topic": 1, "other": 1},
        "evergreen": 1, "transient": 2}
    assert fmod.format_summary(summary) == (
        "labels: 1 labeled page(s) (repo 1, org 1, stack 1, topic 1, other 1); "
        "scope: 1 evergreen, 2 transient")


def test_reindex_report_has_label_line_on_unlabeled_store(tmp_path):
    (tmp_path / "MEMORY.md").write_text("# idx\n- [a](a.md)\n")
    (tmp_path / "a.md").write_text(page("name: a"))
    data = memory_reindex.report_data(memory_reindex.Scan(str(tmp_path)))
    assert data["labels"]["labeled"] == 0
    assert "**labels**: 0 labeled page(s)" in memory_reindex.render_report(data)


def test_page_paths_lists_store_pages(tmp_path):
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("# idx\n")
    (proj / "a.md").write_text(page("name: a"))
    assert fmod.page_paths(str(tmp_path)) == [str(proj / "a.md")]
    assert fmod.page_paths(str(tmp_path / "nope")) == []


def test_parse_fields_strips_quotes():
    text = page(
        'name: "demo: title"',
        "description: 'A: b'",
        "colon_unquoted: A: b",
        "plain: hello world",
        'single_double: "plain"',
        "single_single: 'plain'",
        'empty_double: ""',
        "empty_single: ''",
        "nested_quotes: '\"quoted\"'",
    )
    fm = fmod.find_frontmatter(text)
    fields = fmod.parse_fields(text, fm)
    assert fields["name"] == "demo: title"
    assert fields["description"] == "A: b"
    assert fields["colon_unquoted"] == "A: b"
    assert fields["plain"] == "hello world"
    assert fields["single_double"] == "plain"
    assert fields["single_single"] == "plain"
    assert fields["empty_double"] == ""
    assert fields["empty_single"] == ""
    assert fields["nested_quotes"] == '"quoted"'


def test_page_paths_flat_layout(tmp_path):
    (tmp_path / "layout.yml").write_text("layout: labels\n", encoding="utf-8")
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "MEMORY.md").write_text("# index\n", encoding="utf-8")
    (pages_dir / "a.md").write_text(page("name: a"), encoding="utf-8")
    (pages_dir / "b.md").write_text(page("name: b"), encoding="utf-8")
    (pages_dir / "archive").mkdir()
    (pages_dir / "archive" / "c.md").write_text(page("name: c"), encoding="utf-8")
    assert fmod.page_paths(str(tmp_path)) == [str(pages_dir / "a.md"), str(pages_dir / "b.md")]

