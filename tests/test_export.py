"""okfmem export (#149): one repo's pages as a reviewable bundle that the
receiver imports with the unchanged `inbox push` + `inbox review`."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

import memory_export as mx
import memory_inbox as inbox
from conftest import ROOT
from memory_frontmatter import find_frontmatter, parse_fields, parse_labels

GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
API_KEY_LINE = "api_key = " + "abcd1234efgh5678"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _page(name, labels, body="Body text.\n", typ="feedback", extra=""):
    return (
        f"---\nname: {name}\ndescription: about {name}\ntype: {typ}\n"
        f"labels: [{', '.join(labels)}]\n{extra}---\n\n{body}"
    )


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


RULE_BODY = (
    "Pin the runner image.\r\n\r\nSee [helper](helper.md), [gone](missing.md)"
    " and [docs](https://example.com/x.md).\r\n"
)


@pytest.fixture
def store(tmp_path):
    s = str(tmp_path / "store-a")
    pages = os.path.join(s, "pages")
    _write(
        os.path.join(s, "registry.json"),
        json.dumps(
            {"layout": "labels", "map": {}, "overrides": {},
             "aliases": {"repo:demo": ["repo:demo-old"]}}
        ),
    )
    _write(
        os.path.join(pages, "rule.md"),
        _page("Rule", ["repo:demo", "topic:ci"], RULE_BODY,
              extra="last_accessed: 2026-11-01\naccess_count: 7\npinned: true\n"),
    )
    _write(os.path.join(pages, "helper.md"), _page("Helper", ["repo:demo-old"]))
    _write(os.path.join(pages, "other-repo.md"), _page("Other", ["repo:other", "topic:ci"]))
    _write(os.path.join(pages, "org-only.md"), _page("Org", ["org:acme", "topic:ci"]))
    _write(os.path.join(pages, "me.md"), _page("Me", ["repo:demo"], typ="user"))
    _write(os.path.join(pages, "token.md"),
           _page("Token", ["repo:demo"], f"Use {GITHUB_TOKEN} here.\n"))
    _write(os.path.join(pages, "apikey.md"), _page("Key", ["repo:demo"], API_KEY_LINE + "\n"))
    for name in ("MEMORY.md", "MEMORY-org.md", "STATE.md", "CONTEXT.md"):
        _write(os.path.join(pages, name), _page(name, ["repo:demo"]))
    _write(os.path.join(pages, "archive", "2026", "old.md"), _page("Old", ["repo:demo"]))
    _write(os.path.join(s, "state", "demo.md"), _page("State", ["repo:demo"], typ="state"))
    _write(os.path.join(s, "views", "demo", "pinned.md"), _page("View", ["repo:demo"]))
    return s


def _run(store, out, *extra, cwd=None):
    return mx.main(["--store", store, "--out", out, *extra], cwd=cwd or os.path.dirname(store))


def _bundle_files(out):
    return sorted(os.listdir(out))


def test_repo_export_writes_live_repo_and_alias_pages_plus_manifest(store, tmp_path, capsys):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo") == 0
    assert _bundle_files(out) == ["helper.md", "manifest.json", "rule.md"]


def test_label_selects_any_given_label_and_with_repo_needs_both(store, tmp_path):
    names = lambda **kw: sorted(p["name"] for p in mx.build_bundle(store, **kw)["pages"])  # noqa: E731
    assert names(labels=["topic:ci", "org:acme"]) == ["org-only.md", "other-repo.md", "rule.md"]
    assert names(repo="demo", labels=["topic:ci"]) == ["rule.md"]


def test_neither_repo_nor_label_exits_2_with_one_line(store, tmp_path, capsys):
    out = str(tmp_path / "bundle")
    assert _run(store, out) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "--repo" in err and "--label" in err
    assert not os.path.exists(out)


def test_archived_pages_only_with_archive(store):
    plain = [p["name"] for p in mx.build_bundle(store, repo="demo")["pages"]]
    with_archive = [p["name"] for p in mx.build_bundle(store, repo="demo", archive=True)["pages"]]
    assert "old.md" not in plain
    assert "old.md" in with_archive


def test_index_state_view_and_user_pages_are_never_exported(store, tmp_path, capsys):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo", "--archive") == 0
    files = _bundle_files(out)
    for name in ("MEMORY.md", "MEMORY-org.md", "STATE.md", "CONTEXT.md", "me.md",
                 "demo.md", "pinned.md"):
        assert name not in files
    assert "left out 1 type: user page(s)" in capsys.readouterr().out


def test_secret_pages_are_held_listed_and_printed(store, tmp_path, capsys):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo") == 0
    printed = capsys.readouterr().out
    assert "token.md" not in _bundle_files(out) and "apikey.md" not in _bundle_files(out)
    held = {h["name"]: h["reason"] for h in json.loads(_read(os.path.join(out, "manifest.json")))["held"]}
    assert set(held) == {"token.md", "apikey.md"}
    assert all("secret pattern" in r for r in held.values())
    assert "  held pages/token.md: secret pattern" in printed
    assert "  held pages/apikey.md: secret pattern" in printed
    assert GITHUB_TOKEN not in printed


def test_exported_page_drops_decay_fields_gains_repo_label_keeps_body(store, tmp_path):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo") == 0
    original = _read(os.path.join(store, "pages", "rule.md"))
    for name in ("rule.md", "helper.md"):
        text = _read(os.path.join(out, name))
        fields = parse_fields(text, find_frontmatter(text))
        assert not {"last_accessed", "access_count", "pinned"} & set(fields)
        assert "repo:demo" in parse_labels(text)
    rule = _read(os.path.join(out, "rule.md"))
    assert rule[find_frontmatter(rule)[2]:] == original[find_frontmatter(original)[2]:]
    assert rule.endswith(RULE_BODY)
    assert parse_labels(_read(os.path.join(out, "helper.md"))) == ["repo:demo-old", "repo:demo"]


def test_manifest_lists_sha256_of_each_page_and_unresolved_links(store, tmp_path):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo") == 0
    man = json.loads(_read(os.path.join(out, "manifest.json")))
    assert man["format"] == 1 and man["engine"].startswith("okfmem ")
    assert man["filter"] == {"repo": "demo", "labels": [], "archive": False}
    for entry in man["pages"]:
        with open(os.path.join(out, entry["name"]), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == entry["sha256"]
    assert sorted(p["name"] for p in man["pages"]) == ["helper.md", "rule.md"]
    assert man["unresolved_links"] == [{"page": "rule.md", "target": "missing.md"}]


def test_non_empty_out_is_refused_and_nothing_written(store, tmp_path, capsys):
    out = tmp_path / "busy"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    assert _run(store, str(out), "--repo", "demo") == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and str(out) in err
    assert _bundle_files(str(out)) == ["keep.txt"]


def test_empty_out_is_accepted(store, tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    assert _run(store, str(out), "--repo", "demo") == 0
    assert "manifest.json" in _bundle_files(str(out))


def test_dry_run_writes_nothing_and_json_prints_manifest(store, tmp_path, capsys):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo", "--dry-run", "--json") == 0
    assert not os.path.exists(out)
    captured = capsys.readouterr()
    man = json.loads(captured.out)
    assert sorted(p["name"] for p in man["pages"]) == ["helper.md", "rule.md"]
    assert "would export 2 page(s), held 2" in captured.err


def test_nothing_selected_exits_1_and_writes_nothing(store, tmp_path):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "nobody") == 1
    assert not os.path.exists(out)


def test_archived_page_with_a_live_page_name_is_held(store, tmp_path):
    _write(os.path.join(store, "pages", "archive", "2025", "rule.md"), _page("Rule", ["repo:demo"]))
    _write(os.path.join(store, "pages", "archive", "2024", "Helper.md"), _page("H", ["repo:demo"]))
    bundle = mx.build_bundle(store, repo="demo", archive=True)
    assert [p["path"] for p in bundle["pages"] if p["name"] == "rule.md"] == ["pages/rule.md"]
    held = {h["path"] for h in bundle["held"]}
    assert {"pages/archive/2025/rule.md", "pages/archive/2024/Helper.md"} <= held


def test_printed_import_command_is_the_exact_inbox_line(store, tmp_path, capsys):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo") == 0
    expected = (f"okfmem inbox push {shlex.quote(out)} --source export --repo demo"
                " && okfmem inbox review")
    assert f"  {expected}" in capsys.readouterr().out.splitlines()


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
def test_import_command_names_the_registered_checkouts_origin(store, tmp_path):
    checkout = str(tmp_path / "demo-checkout")
    os.makedirs(checkout)
    _git(checkout, "init", "-q")
    _git(checkout, "remote", "add", "origin", "https://github.com/Acme/Demo.git")
    reg_path = os.path.join(store, "registry.json")
    reg = json.loads(_read(reg_path))
    reg["map"] = {checkout: "demo"}
    _write(reg_path, json.dumps(reg))
    assert mx.repo_slug(store, "demo", cwd=str(tmp_path)) == "acme/demo"
    assert mx.repo_slug(store, "other", cwd=str(tmp_path)) == "other"


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
def test_round_trip_through_inbox_push_and_review(store, tmp_path):
    out = str(tmp_path / "bundle")
    assert _run(store, out, "--repo", "demo") == 0

    origin = str(tmp_path / "b.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    b = str(tmp_path / "store-b")
    _git(str(tmp_path), "clone", "-q", origin, b)
    for key, value in (("user.email", "t@e.st"), ("user.name", "tester"),
                       ("commit.gpgsign", "false")):
        _git(b, "config", key, value)
    _write(os.path.join(b, "registry.json"), json.dumps(
        {"layout": "labels", "overrides": {},
         "map": {os.path.join(str(tmp_path), "nowhere", "demo"): "demo"}}))
    _write(os.path.join(b, ".gitignore"), "views/\n")
    _write(os.path.join(b, "pages", "local.md"), _page("Local", ["repo:demo"]))
    _git(b, "add", "-A")
    _git(b, "commit", "-qm", "seed")
    _git(b, "push", "-q", "-u", "origin", "HEAD")

    code, ref = inbox.push(b, out, "export", repo="acme/demo", out=lambda _: None)
    assert code == 0 and ref
    counts = inbox.review(b, lambda push_, page, flags: ("accept", None),
                          lambda _: pytest.fail("asked for project"), out=lambda _: None)
    assert counts["accepted"] == 2

    for name in ("rule.md", "helper.md"):
        assert "repo:demo" in parse_labels(_read(os.path.join(b, "pages", name)))
    view = _read(os.path.join(b, "views", "demo", "MEMORY.md"))
    assert "(pages/rule.md)" in view and "(pages/helper.md)" in view


def test_dispatcher_routes_export(store, tmp_path):
    out = str(tmp_path / "bundle")
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "okfmem"), "export", "--repo", "demo",
         "--out", out, "--store", store, "--dry-run"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert "would export 2 page(s)" in r.stdout
    assert not os.path.exists(out)
