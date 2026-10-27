"""Tests for dynamic MEMORY.md view projection from label queries (#99)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import memory_consolidate as mc
import memory_init as mi
import memory_reindex as mr


def _make_repo(path, remote=None, markers=()):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote],
            check=True,
        )
    for m in markers:
        with open(os.path.join(path, m), "w", encoding="utf-8") as f:
            f.write("# marker\n")
    return os.path.normpath(str(path))


def _make_store(path, marker=True):
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "pages"), exist_ok=True)
    if marker:
        with open(os.path.join(path, ".layout"), "w", encoding="utf-8") as f:
            f.write("layout: labels\n")
    with open(os.path.join(path, "registry.json"), "w", encoding="utf-8") as f:
        json.dump({"map": {}, "overrides": {}, "aliases": {}}, f)
    return str(path)


def _write_page(store, slug, labels, title=None, desc=None, importance=1, recency="2026-10-01", archive=False):
    if archive:
        pdir = os.path.join(store, "pages", "archive")
    else:
        pdir = os.path.join(store, "pages")
    os.makedirs(pdir, exist_ok=True)
    fpath = os.path.join(pdir, f"{slug}.md")
    t = title or slug.replace("-", " ").title()
    d = desc or f"Description for {slug}"
    labels_str = "[" + ", ".join(labels) + "]"
    content = (
        "---\n"
        f"name: {t}\n"
        f"description: {d}\n"
        f"importance: {importance}\n"
        f"last_accessed: {recency}\n"
        f"labels: {labels_str}\n"
        "---\n\n"
        f"# {t}\n\nContent for {slug}\n"
    )
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)
    return fpath


def test_compile_matches_repo_and_alias_labels_case_insensitively(tmp_path):
    store = _make_store(tmp_path / "store")
    _write_page(store, "repo-page", ["repo:offlinecv"])
    _write_page(store, "alias-page", ["repo:other"])

    mi.compile_view(store, "OfflineCV", aliases=["REPO:OTHER"])

    memory = Path(store, "views", "OfflineCV", "MEMORY.md").read_text(encoding="utf-8")
    assert "pages/repo-page.md" in memory
    assert "pages/alias-page.md" in memory


def test_owner_from_remote_url_formats():
    assert mi._owner_from_remote("https://github.com/116-ideas/toran.git") == "116-ideas"
    assert mi._owner_from_remote("https://github.com/116-ideas/toran") == "116-ideas"
    assert mi._owner_from_remote("https://github.com/116-ideas/toran/") == "116-ideas"
    assert mi._owner_from_remote("git@github.com:116-ideas/toran.git") == "116-ideas"
    assert mi._owner_from_remote("ssh://git@host:22/org/repo.git") == "org"
    assert mi._owner_from_remote("https://github.com/repo.git") is None
    assert mi._owner_from_remote("git@host:repo.git") is None
    assert mi._owner_from_remote("/srv/git/repo.git") is None
    assert mi._owner_from_remote("C:\\git\\repo.git") is None
    assert mi._owner_from_remote("") is None
    assert mi._owner_from_remote(None) is None


def test_detect_stack(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    assert mi._detect_stack(str(d)) == []

    (d / "pyproject.toml").write_text("[project]\n")
    assert mi._detect_stack(str(d)) == ["python"]

    (d / "package.json").write_text("{}\n")
    assert mi._detect_stack(str(d)) == ["python", "typescript"]

    (d / "go.mod").write_text("module foo\n")
    (d / "Cargo.toml").write_text("[package]\n")
    assert mi._detect_stack(str(d)) == ["go", "python", "rust", "typescript"]


def test_resolve_project_json_fields_and_compile_gate(tmp_path):
    repo = _make_repo(
        tmp_path / "toran",
        remote="https://github.com/116-ideas/toran.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")
    _write_page(store, "p1", ["repo:toran"], title="Toran Architecture")

    # 1. Without --compile: read-only, nothing written
    res = mi.resolve_project(store, cwd=repo, compile=False)
    assert res["repo"] == "toran"
    assert res["org"] == "116-ideas"
    assert res["stack"] == ["python"]
    assert "repo:toran" in res["matched_labels"]
    assert "org:116-ideas" in res["matched_labels"]
    assert "stack:python" in res["matched_labels"]
    assert res["view_path"] == os.path.join(store, "views", "toran")
    assert res["view"] == res["view_path"]
    assert not os.path.exists(res["view_path"])

    # 2. With compile=True: writes views/toran/MEMORY.md
    res_compiled = mi.resolve_project(store, cwd=repo, compile=True)
    assert res_compiled["repo"] == "toran"
    assert os.path.isdir(res_compiled["view_path"])
    mem_file = os.path.join(res_compiled["view_path"], "MEMORY.md")
    assert os.path.isfile(mem_file)
    with open(mem_file, "r", encoding="utf-8") as f:
        mem_text = f.read()
    assert "Toran Architecture" in mem_text
    assert "pages/p1.md" in mem_text


def test_tiered_view_and_archive_exclusion(tmp_path):
    repo = _make_repo(
        tmp_path / "toran",
        remote="https://github.com/116-ideas/toran.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")
    _write_page(store, "repo-page", ["repo:toran"], title="Repo Memory", importance=3)
    _write_page(store, "org-page", ["org:116-ideas"], title="Org Standards", importance=4)
    _write_page(store, "stack-page", ["stack:python"], title="Python Guidelines", importance=5)
    _write_page(store, "other-repo", ["repo:other"], title="Other Repo")
    _write_page(store, "archived-page", ["repo:toran"], title="Old Archived", archive=True)

    mi.resolve_project(store, cwd=repo, compile=True)
    view_dir = os.path.join(store, "views", "toran")
    with open(os.path.join(view_dir, "MEMORY.md"), "r", encoding="utf-8") as f:
        content = f.read()

    # Archived and other repo pages excluded
    assert "Other Repo" not in content
    assert "Old Archived" not in content

    # Tiered order: Repo first, then Org, then Stack
    pos_repo = content.index("Repo Memory")
    pos_org = content.index("Org Standards")
    pos_stack = content.index("Python Guidelines")
    assert pos_repo < pos_org < pos_stack


def test_relative_links_and_reindex_verify_exits_zero(tmp_path):
    repo = _make_repo(
        tmp_path / "toran",
        remote="https://github.com/116-ideas/toran.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")
    _write_page(store, "page1", ["repo:toran"], title="Page One")
    _write_page(store, "page2", ["org:116-ideas"], title="Page Two")

    mi.resolve_project(store, cwd=repo, compile=True)
    view_dir = os.path.join(store, "views", "toran")

    # Filesystem links are relative
    pages_link = os.path.join(view_dir, "pages")
    state_link = os.path.join(view_dir, "STATE.md")
    assert os.path.islink(pages_link) or mi._is_junction(pages_link) or os.path.isdir(pages_link)
    if os.path.islink(pages_link):
        target = os.readlink(pages_link)
        assert not os.path.isabs(target)
        assert target.replace("\\", "/") == "../../pages"
    if os.path.islink(state_link):
        target = os.readlink(state_link)
        assert not os.path.isabs(target)
        assert target.replace("\\", "/") == "../../state/toran.md"

    # okfmem reindex --verify exits 0 on the compiled view
    assert mr.main(["--verify", view_dir]) == 0


def test_registry_overrides_org_and_stack(tmp_path):
    repo = _make_repo(
        tmp_path / "myrepo",
        remote="https://github.com/detected-org/myrepo.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")

    # Set overrides in registry.json
    reg_path = os.path.join(store, "registry.json")
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "map": {},
                "overrides": {
                    repo: {
                        "org": "custom-org",
                        "stack": ["rust", "go"],
                    }
                },
                "aliases": {},
            },
            f,
        )

    res = mi.resolve_project(store, cwd=repo, compile=False)
    assert res["org"] == "custom-org"
    assert res["stack"] == ["go", "rust"]
    assert "org:custom-org" in res["matched_labels"]
    assert "stack:go" in res["matched_labels"]
    assert "stack:rust" in res["matched_labels"]
    assert "org:detected-org" not in res["matched_labels"]
    assert "stack:python" not in res["matched_labels"]


def test_registry_aliases_one_way_and_non_transitive(tmp_path):
    repo_toran = _make_repo(tmp_path / "toran", remote="https://github.com/116/toran.git")
    repo_gaal = _make_repo(tmp_path / "gaal", remote="https://github.com/116/gaal.git")
    store = _make_store(tmp_path / "store")

    reg_path = os.path.join(store, "registry.json")
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "map": {},
                "overrides": {},
                "aliases": {
                    "repo:toran": ["repo:gaal"],
                    "repo:gaal": ["repo:unrelated"],
                },
            },
            f,
        )

    _write_page(store, "gaal-page", ["repo:gaal"], title="Gaal Architecture")
    _write_page(store, "unrelated-page", ["repo:unrelated"], title="Unrelated Architecture")
    _write_page(store, "toran-page", ["repo:toran"], title="Toran Architecture")

    # toran view includes gaal-page due to alias, but NOT unrelated-page (non-transitive)
    res_toran = mi.resolve_project(store, cwd=repo_toran, compile=True)
    assert "repo:gaal" in res_toran["matched_labels"]
    assert "repo:unrelated" not in res_toran["matched_labels"]
    view_toran = os.path.join(store, "views", "toran", "MEMORY.md")
    with open(view_toran, "r", encoding="utf-8") as f:
        toran_content = f.read()
    assert "Gaal Architecture" in toran_content
    assert "Toran Architecture" in toran_content
    assert "Unrelated Architecture" not in toran_content

    # gaal view does NOT include toran-page (one-way)
    mi.resolve_project(store, cwd=repo_gaal, compile=True)
    view_gaal = os.path.join(store, "views", "gaal", "MEMORY.md")
    with open(view_gaal, "r", encoding="utf-8") as f:
        gaal_content = f.read()
    assert "Toran Architecture" not in gaal_content


def test_refusal_on_store_with_projects_and_no_marker(tmp_path):
    store = tmp_path / "legacy-store"
    store.mkdir()
    (store / "projects" / "p1").mkdir(parents=True)
    (store / "registry.json").write_text(json.dumps({"map": {}, "overrides": {}}))

    # check_store_layout raises ValueError naming okfmem migrate-to-labels
    with pytest.raises(ValueError) as exc:
        mi.check_store_layout(str(store))
    assert "okfmem migrate-to-labels" in str(exc.value)

    # resolve_project with compile=True raises ValueError
    with pytest.raises(ValueError) as exc:
        mi.resolve_project(str(store), compile=True)
    assert "okfmem migrate-to-labels" in str(exc.value)

    # CLI okfmem init --resolve-project exits non-zero and prints message
    cmd = [sys.executable, str(Path(mi.__file__)), "--resolve-project", "--store", str(store)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    assert p.returncode != 0
    assert "okfmem migrate-to-labels" in p.stderr

    # Adding layout marker makes it pass
    (store / ".layout").write_text("layout: labels\n")
    mi.check_store_layout(str(store))


def test_overflow_beyond_budget_spills_to_lane_indexes(tmp_path):
    repo = _make_repo(
        tmp_path / "bigrepo",
        remote="https://github.com/116-ideas/bigrepo.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")

    # Generate 150 stack pages and 50 org pages to exceed budget
    for i in range(120):
        _write_page(store, f"stack-p{i:03d}", ["stack:python"], title=f"Python Rule {i:03d}", desc=f"Hook for python rule {i:03d} with extra detail to consume bytes " * 2)
    for i in range(50):
        _write_page(store, f"org-p{i:03d}", ["org:116-ideas"], title=f"Org Rule {i:03d}", desc=f"Hook for org rule {i:03d} with extra detail to consume bytes " * 2)
    for i in range(10):
        _write_page(store, f"repo-p{i:03d}", ["repo:bigrepo"], title=f"Repo Rule {i:03d}", desc=f"Hook for repo rule {i:03d}")

    mi.resolve_project(store, cwd=repo, compile=True)
    view_dir = os.path.join(store, "views", "bigrepo")
    mem_file = os.path.join(view_dir, "MEMORY.md")
    with open(mem_file, "r", encoding="utf-8") as f:
        mem_text = f.read()

    # Budget enforced: <= 8192 bytes and <= 200 lines
    lines = mem_text.splitlines()
    assert len(lines) <= 200
    assert len(mem_text.encode("utf-8")) <= 8192

    # Overflow spilled to lane index with covers clause
    assert "covers" in mem_text
    assert os.path.isfile(os.path.join(view_dir, "MEMORY-lane-stack.md"))

    # Link integrity passes
    assert mr.main(["--verify", view_dir]) == 0


def test_idempotent_recompile(tmp_path):
    repo = _make_repo(
        tmp_path / "idemrepo",
        remote="https://github.com/116-ideas/idemrepo.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")
    _write_page(store, "page-a", ["repo:idemrepo"], title="Page A")
    _write_page(store, "page-b", ["stack:python"], title="Page B")

    # Run 1
    mi.resolve_project(store, cwd=repo, compile=True)
    view_dir = os.path.join(store, "views", "idemrepo")
    with open(os.path.join(view_dir, "MEMORY.md"), "r", encoding="utf-8") as f:
        content_1 = f.read()

    # Run 2
    mi.resolve_project(store, cwd=repo, compile=True)
    with open(os.path.join(view_dir, "MEMORY.md"), "r", encoding="utf-8") as f:
        content_2 = f.read()

    assert content_1 == content_2
    assert mr.main(["--verify", view_dir]) == 0


def test_no_remote_and_no_matching_pages(tmp_path):
    repo = _make_repo(tmp_path / "noremote", markers=["Cargo.toml"])
    store = _make_store(tmp_path / "store")

    # Case 1: no matching pages in store
    res = mi.resolve_project(store, cwd=repo, compile=True)
    assert res["repo"] == "noremote"
    assert res["org"] is None
    assert res["stack"] == ["rust"]
    view_dir = res["view_path"]
    assert os.path.isfile(os.path.join(view_dir, "MEMORY.md"))
    with open(os.path.join(view_dir, "MEMORY.md"), "r", encoding="utf-8") as f:
        assert f.read() == "# MEMORY — noremote\n"
    assert mr.main(["--verify", view_dir]) == 0


def test_stop_hook_consolidate_refreshes_view(tmp_path, monkeypatch):
    repo = _make_repo(
        tmp_path / "hookrepo",
        remote="https://github.com/116-ideas/hookrepo.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")
    _write_page(store, "hook-page", ["repo:hookrepo"], title="Hook Page")

    # Hook runs consolidate with --stdin-hook payload specifying cwd
    payload = json.dumps({"cwd": repo, "transcript_path": None})
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"read": lambda self: payload})())

    mc.main(["--store", store, "--stdin-hook", "--no-commit", "--today", "2026-10-26"])

    view_dir = os.path.join(store, "views", "hookrepo")
    assert os.path.isfile(os.path.join(view_dir, "MEMORY.md"))
    with open(os.path.join(view_dir, "MEMORY.md"), "r", encoding="utf-8") as f:
        content = f.read()
    assert "Hook Page" in content


def test_stop_hook_consolidate_preserves_stdin_payload_with_projects(tmp_path, monkeypatch):
    repo = _make_repo(
        tmp_path / "hookrepo",
        remote="https://github.com/116-ideas/hookrepo.git",
        markers=["pyproject.toml"],
    )
    store = _make_store(tmp_path / "store")
    os.makedirs(os.path.join(store, "projects", "hookrepo"), exist_ok=True)
    _write_page(store, "hook-page", ["repo:hookrepo"], title="Hook Page")

    payload = json.dumps({"cwd": repo, "transcript_path": None})
    reads = [payload, ""]
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"read": lambda self: reads.pop(0) if reads else ""})())

    mc.main(["--store", store, "--stdin-hook", "--no-commit", "--today", "2026-10-26"])

    view_dir = os.path.join(store, "views", "hookrepo")
    assert os.path.isfile(os.path.join(view_dir, "MEMORY.md"))
    with open(os.path.join(view_dir, "MEMORY.md"), "r", encoding="utf-8") as f:
        content = f.read()
    assert "Hook Page" in content


def test_resolve_on_marked_store_never_falls_back_to_projects(tmp_path):
    # #112: a machine that pulled a migrated store has no views/ yet, while
    # projects/<repo>/ still holds the pre-migration index and a STATE.md.
    repo = _make_repo(tmp_path / "toran")
    store = _make_store(tmp_path / "store")
    legacy = os.path.join(store, "projects", "toran")
    os.makedirs(legacy)
    for f in ("MEMORY.md", "STATE.md"):
        with open(os.path.join(legacy, f), "w", encoding="utf-8") as fh:
            fh.write("# old\n")

    res = mi.resolve_project(store, cwd=repo, compile=False)
    assert res["memory_dir"] == os.path.join(store, "views", "toran")
    assert res["state"] == "uninitialized"
    assert not os.path.exists(res["view_path"])

    res = mi.resolve_project(store, cwd=repo, compile=True)
    assert res["memory_dir"] == res["view_path"]
    assert res["state"] == "ready"


def test_resolve_on_legacy_store_keeps_projects_fallback(tmp_path):
    repo = _make_repo(tmp_path / "toran")
    store = _make_store(tmp_path / "store", marker=False)
    legacy = os.path.join(store, "projects", "toran")
    os.makedirs(legacy)
    for f in ("MEMORY.md", "STATE.md"):
        with open(os.path.join(legacy, f), "w", encoding="utf-8") as fh:
            fh.write("# old\n")

    res = mi.resolve_project(store, cwd=repo, compile=False)
    assert res["memory_dir"] == legacy
    assert res["state"] == "ready"


# #121: a plain STATE.md in a view (an older init's seed stub, or a Windows
# copy fallback) must not shadow state/<repo>.md.

def _state(modified=None, body="real state"):
    head = "---\ntype: state\n" + (f"modified: {modified}\n" if modified else "") + "---\n\n"
    return head + body + "\n"


def test_compile_replaces_a_stub_state_with_the_real_one(tmp_path):
    store = _make_store(tmp_path / "store")
    os.makedirs(os.path.join(store, "state"))
    real = os.path.join(store, "state", "toran.md")
    with open(real, "w", encoding="utf-8") as f:
        f.write(_state("2026-10-27T02:00:00Z"))
    view = os.path.join(store, "views", "toran")
    os.makedirs(view)
    with open(os.path.join(view, "STATE.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: toran-state\ntype: state\n---\n\n# STATE — toran\n")

    mi.compile_view(store, "toran")
    with open(os.path.join(view, "STATE.md"), encoding="utf-8") as f:
        assert "real state" in f.read()
    with open(real, encoding="utf-8") as f:
        assert "real state" in f.read()


def test_compile_rescues_a_newer_state_saved_into_the_view(tmp_path):
    store = _make_store(tmp_path / "store")
    os.makedirs(os.path.join(store, "state"))
    real = os.path.join(store, "state", "toran.md")
    with open(real, "w", encoding="utf-8") as f:
        f.write(_state("2026-10-27T02:00:00Z", "old state"))
    view = os.path.join(store, "views", "toran")
    os.makedirs(view)
    with open(os.path.join(view, "STATE.md"), "w", encoding="utf-8") as f:
        f.write(_state("2026-10-27T09:00:00Z", "newer state saved in the view"))

    mi.compile_view(store, "toran")
    with open(real, encoding="utf-8") as f:
        assert "newer state saved in the view" in f.read()
    with open(os.path.join(view, "STATE.md"), encoding="utf-8") as f:
        assert "newer state saved in the view" in f.read()
