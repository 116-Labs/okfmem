"""Attached read stores beside the home store (#139)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import memory_init as mi
import memory_pull as mp
import memory_reindex as mr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

needs_git = pytest.mark.skipif(shutil.which("git") is None,
                               reason="git binary not available")


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r


def _make_repo(path, remote="https://github.com/116-ideas/toran.git"):
    os.makedirs(path, exist_ok=True)
    _git(str(path), "init", "-q")
    _git(str(path), "remote", "add", "origin", remote)
    Path(path, "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return os.path.normpath(str(path))


def _page_text(slug, labels, title=None, desc=None, recency="2026-10-01",
               importance=1):
    t = title or slug.replace("-", " ").title()
    return (
        "---\n"
        f"name: {t}\n"
        f"description: {desc or f'Description for {slug}'}\n"
        f"importance: {importance}\n"
        f"last_accessed: {recency}\n"
        f"labels: [{', '.join(labels)}]\n"
        "---\n\n"
        f"# {t}\n\nContent for {slug}\n"
    )


def _write_page(store, slug, labels, **kw):
    pdir = Path(store, "pages")
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{slug}.md"
    path.write_text(_page_text(slug, labels, **kw), encoding="utf-8")
    return path


def _make_store(path, stores=()):
    Path(path, "pages").mkdir(parents=True, exist_ok=True)
    Path(path, ".layout").write_text("layout: labels\n", encoding="utf-8")
    reg = {"map": {}, "overrides": {}, "aliases": {}}
    if stores:
        reg["stores"] = list(stores)
    Path(path, "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    return str(path)


def _snapshot(root):
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                snap[p] = f.read()
    return snap


# ---------------------------------------------------------------------------
# attached_stores
# ---------------------------------------------------------------------------

def test_attached_stores_skips_malformed_duplicate_and_home_entries(tmp_path):
    home = tmp_path / "home"
    team = tmp_path / "team"
    store = _make_store(home, stores=[
        {"name": "team", "path": str(team), "remote": "acme/team-memory"},
        {"name": "Team", "path": str(tmp_path / "dup")},
        {"name": "self", "path": str(home)},
        {"name": "../escape", "path": str(tmp_path / "x")},
        {"name": "nopath"},
        "not-a-dict",
        {"name": "tilde", "path": "~/okfmem-tilde"},
    ])

    got = mi.attached_stores(store)

    assert [a["name"] for a in got] == ["team", "tilde"]
    assert got[0] == {"name": "team", "path": os.path.abspath(str(team)),
                      "remote": "acme/team-memory"}
    assert got[1]["path"] == os.path.abspath(os.path.expanduser("~/okfmem-tilde"))
    assert got[1]["remote"] is None


def test_relative_attached_path_resolves_against_home_store_not_cwd(tmp_path, monkeypatch):
    store = _make_store(tmp_path / "home", stores=[
        {"name": "team", "path": "../okfmem-team"},
        {"name": "nested", "path": "sub/inner"},
    ])
    elsewhere = tmp_path / "src" / "app"
    elsewhere.mkdir(parents=True)

    for cwd in (elsewhere, tmp_path / "home"):
        monkeypatch.chdir(cwd)
        got = mi.attached_stores(store)
        assert [a["path"] for a in got] == [
            os.path.abspath(str(tmp_path / "okfmem-team")),
            os.path.abspath(str(tmp_path / "home" / "sub" / "inner")),
        ]


def test_attached_stores_absent_or_not_a_list_is_empty(tmp_path):
    store = _make_store(tmp_path / "home")
    assert mi.attached_stores(store) == []
    assert mi.attached_stores(store, {"stores": {"name": "team"}}) == []


def test_store_remote_url_expands_github_shorthand_only():
    assert mi.store_remote_url("acme/team-memory") == "https://github.com/acme/team-memory.git"
    assert mi.store_remote_url("git@host:acme/x.git") == "git@host:acme/x.git"
    assert mi.store_remote_url(None) is None


def test_registry_write_keeps_the_stores_list(tmp_path):
    store = _make_store(tmp_path / "home", stores=[
        {"name": "team", "path": "~/okfmem-team"}])

    _path, _changed, merged = mi.write_registry(
        store, {"map": {}, "overrides": {}}, dry_run=False)

    assert merged["stores"] == [{"name": "team", "path": "~/okfmem-team"}]
    on_disk = json.loads(Path(store, "registry.json").read_text(encoding="utf-8"))
    assert on_disk["stores"] == merged["stores"]


# ---------------------------------------------------------------------------
# compiled view
# ---------------------------------------------------------------------------

def test_view_holds_pages_from_both_stores_within_budget_and_verifies(tmp_path):
    repo = _make_repo(tmp_path / "toran")
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    _write_page(store, "home-repo", ["repo:toran"], title="Home Repo Page")
    _write_page(team, "team-repo", ["repo:toran"], title="Team Repo Page")
    _write_page(team, "team-org", ["org:116-ideas"], title="Team Org Page")
    _write_page(team, "team-other", ["repo:other"], title="Team Other Page")

    res = mi.resolve_project(store, cwd=repo, compile=True)

    view = res["view_path"]
    memory = Path(view, "MEMORY.md").read_text(encoding="utf-8")
    assert "(pages/home-repo.md)" in memory
    assert "[Team Repo Page](stores/team/team-repo.md)" in memory
    assert "(stores/team/team-org.md)" in memory
    assert "Team Other Page" not in memory
    team_line = next(ln for ln in memory.splitlines() if "team-repo.md" in ln)
    home_line = next(ln for ln in memory.splitlines() if "home-repo.md" in ln)
    assert team_line.endswith("(store: team)")
    assert "store:" not in home_line
    assert memory.index("Team Repo Page") < memory.index("Team Org Page")
    assert "Content for team-repo" in Path(
        view, "stores", "team", "team-repo.md").read_text(encoding="utf-8")
    assert len(memory.splitlines()) <= 200
    assert len(memory.encode("utf-8")) <= 8192
    assert mr.main(["--verify", view]) == 0


def test_slug_in_both_stores_appears_once_from_the_home_store(tmp_path):
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    _write_page(store, "conventions", ["repo:toran"], title="Home Conventions")
    _write_page(team, "conventions", ["repo:toran"], title="Team Conventions",
                importance=5)

    mi.compile_view(store, "toran")

    memory = Path(store, "views", "toran", "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("conventions.md") == 1
    assert "[Home Conventions](pages/conventions.md)" in memory
    assert "Team Conventions" not in memory


def test_slug_clash_resolves_to_the_home_page_across_tiers(tmp_path):
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    _write_page(store, "style", ["stack:python"], title="Home Style")
    _write_page(team, "style", ["repo:toran"], title="Team Style")

    mi.compile_view(store, "toran", stack=["python"])

    memory = Path(store, "views", "toran", "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("style.md") == 1
    assert "[Home Style](pages/style.md)" in memory


def test_attached_page_shows_when_the_home_page_of_that_slug_is_not_in_view(tmp_path):
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    _write_page(store, "deploy", ["repo:other"], title="Other Deploy")
    _write_page(team, "deploy", ["repo:toran"], title="Team Deploy")

    mi.compile_view(store, "toran")

    memory = Path(store, "views", "toran", "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("deploy.md") == 1
    assert "[Team Deploy](stores/team/deploy.md)" in memory


def test_first_attached_store_wins_a_slug_clash_between_attached_stores(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    store = _make_store(tmp_path / "home", stores=[
        {"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}])
    _write_page(a, "shared", ["repo:toran"], title="From A")
    _write_page(b, "shared", ["repo:toran"], title="From B")

    mi.compile_view(store, "toran")

    memory = Path(store, "views", "toran", "MEMORY.md").read_text(encoding="utf-8")
    assert "[From A](stores/a/shared.md)" in memory
    assert "From B" not in memory


def test_merged_view_over_budget_spills_to_lanes_and_verifies(tmp_path):
    repo = _make_repo(tmp_path / "toran")
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    for i in range(10):
        _write_page(store, f"home-p{i:03d}", ["repo:toran"])
    for i in range(120):
        _write_page(team, f"team-stack-{i:03d}", ["stack:python"],
                    desc=f"Team python rule {i:03d} with detail to spend bytes " * 2)

    res = mi.resolve_project(store, cwd=repo, compile=True)

    view = res["view_path"]
    memory = Path(view, "MEMORY.md").read_text(encoding="utf-8")
    assert len(memory.splitlines()) <= 200
    assert len(memory.encode("utf-8")) <= 8192
    lane = Path(view, "MEMORY-lane-stack.md").read_text(encoding="utf-8")
    assert "(stores/team/team-stack-000.md)" in lane
    assert mr.main(["--verify", view]) == 0


def test_state_comes_only_from_the_home_store(tmp_path):
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    Path(store, "state").mkdir()
    Path(store, "state", "toran.md").write_text("home state\n", encoding="utf-8")
    Path(team, "state").mkdir(parents=True)
    Path(team, "state", "toran.md").write_text("TEAM STATE\n", encoding="utf-8")
    _write_page(team, "team-repo", ["repo:toran"])
    Path(team, "pages", "STATE.md").write_text("TEAM PAGES STATE\n", encoding="utf-8")

    mi.compile_view(store, "toran")

    view = Path(store, "views", "toran")
    assert (view / "STATE.md").read_text(encoding="utf-8") == "home state\n"
    assert "STATE.md" not in (view / "MEMORY.md").read_text(encoding="utf-8")


def test_detached_store_link_is_removed_on_recompile(tmp_path):
    team = tmp_path / "team"
    store = _make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}])
    _write_page(team, "team-repo", ["repo:toran"])
    mi.compile_view(store, "toran")
    link = Path(store, "views", "toran", "stores", "team")
    assert (link / "team-repo.md").is_file()
    if not (link.is_symlink() or mi._is_junction(str(link))):
        pytest.skip("copy fallback: a copied store is left in place")

    _make_store(store)
    mi.compile_view(store, "toran")

    assert not os.path.lexists(link)
    assert (team / "pages" / "team-repo.md").is_file()
    memory = Path(store, "views", "toran", "MEMORY.md").read_text(encoding="utf-8")
    assert "stores/team" not in memory


def test_missing_attached_store_is_skipped(tmp_path):
    store = _make_store(tmp_path / "home", stores=[
        {"name": "team", "path": str(tmp_path / "not-cloned")}])
    _write_page(store, "home-repo", ["repo:toran"])

    mi.compile_view(store, "toran")

    view = Path(store, "views", "toran")
    assert "pages/home-repo.md" in (view / "MEMORY.md").read_text(encoding="utf-8")
    assert not (view / "stores").exists()
    assert mr.main(["--verify", str(view)]) == 0


# ---------------------------------------------------------------------------
# consolidate
# ---------------------------------------------------------------------------

def _aged_page(slug):
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {slug} page\n"
        "type: project\n"
        "created: 2026-01-01\n"
        "last_accessed: 2026-01-01\n"
        "access_count: 0\n"
        "labels: [repo:toran]\n"
        "---\n\n"
        f"# {slug}\n\nBody.\n"
    )


def test_consolidate_never_edits_an_attached_store(tmp_path):
    team = tmp_path / "team"
    store = Path(_make_store(tmp_path / "home", stores=[{"name": "team", "path": str(team)}]))
    (store / "pages" / "aged.md").write_text(_aged_page("aged"), encoding="utf-8")
    (store / "decay_state.json").write_text('{"epoch": "2026-01-01"}\n', encoding="utf-8")
    (team / "pages").mkdir(parents=True)
    (team / "pages" / "team-aged.md").write_text(_aged_page("team-aged"), encoding="utf-8")
    (team / "pages" / "team-read.md").write_text(_aged_page("team-read"), encoding="utf-8")
    mi.compile_view(str(store), "toran")
    transcript = tmp_path / "session.jsonl"
    view_page = store / "views" / "toran" / "stores" / "team" / "team-read.md"
    transcript.write_text(
        f"read {view_page}\nread {team / 'pages' / 'team-read.md'}\n", encoding="utf-8")
    before = _snapshot(str(team))

    env = dict(os.environ, OKFMEM_NO_STATUS="1")
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "memory_consolidate.py"),
         "--store", str(store), "--no-commit", "--today", "2026-07-31",
         "--transcript", str(transcript)],
        capture_output=True, text=True, env=env, cwd=ROOT, check=True)

    assert (store / "pages" / "archive" / "2026" / "aged.md").is_file()
    assert _snapshot(str(team)) == before
    assert not (team / "pages" / "archive").exists()


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _config(repo):
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def _seed_clone(tmp_path, name, files):
    origin = str(tmp_path / f"{name}-origin.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    clone = str(tmp_path / name)
    _git(str(tmp_path), "clone", "-q", origin, clone)
    _config(clone)
    for rel, text in files.items():
        Path(clone, rel).parent.mkdir(parents=True, exist_ok=True)
        Path(clone, rel).write_text(text, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "seed")
    _git(clone, "push", "-q", "-u", "origin", "HEAD")
    return origin, clone


def _advance(tmp_path, origin, name, rel, text):
    w = str(tmp_path / f"w-{name}")
    _git(str(tmp_path), "clone", "-q", origin, w)
    _config(w)
    Path(w, rel).parent.mkdir(parents=True, exist_ok=True)
    Path(w, rel).write_text(text, encoding="utf-8")
    _git(w, "add", "-A")
    _git(w, "commit", "-qm", f"advance {name}")
    _git(w, "push", "-q", "origin", "HEAD")


def _pull_fixture(tmp_path):
    team_origin, team = _seed_clone(tmp_path, "team", {"pages/t.md": "v1\n"})
    stores = [
        {"name": "gone", "path": str(tmp_path / "gone"), "remote": "acme/gone"},
        {"name": "team", "path": team},
    ]
    reg = json.dumps({"map": {}, "overrides": {}, "stores": stores})
    _home_origin, home = _seed_clone(tmp_path, "home", {
        "registry.json": reg, "pages/h.md": "home\n", ".layout": "layout: labels\n"})
    _advance(tmp_path, team_origin, "team", "pages/t.md", "v2\n")
    return home, team


@needs_git
def test_pull_covers_every_attached_store_past_a_failing_one(tmp_path):
    home, team = _pull_fixture(tmp_path)

    results = mp.pull_attached(home)

    assert [a["name"] for a, _ in results] == ["gone", "team"]
    gone = results[0][1]
    assert gone["pulled"] is False and gone["conflict"] is False
    assert "not cloned" in gone["reason"]
    assert "git clone https://github.com/acme/gone.git" in gone["reason"]
    assert results[1][1]["pulled"] is True
    assert Path(team, "pages", "t.md").read_text(encoding="utf-8") == "v2\n"


@needs_git
def test_pull_keeps_going_when_one_store_raises(tmp_path, monkeypatch):
    home, team = _pull_fixture(tmp_path)
    Path(tmp_path, "gone").mkdir()
    real = mp.pull_store

    def flaky(path, timeout=mp.DEFAULT_TIMEOUT, token=None):
        if path.endswith("gone"):
            raise RuntimeError("boom")
        return real(path, timeout=timeout, token=token)

    monkeypatch.setattr(mp, "pull_store", flaky)

    results = mp.pull_attached(home)

    assert "boom" in results[0][1]["reason"]
    assert results[1][1]["pulled"] is True


@needs_git
def test_pull_main_reports_each_store_and_exits_zero(tmp_path, monkeypatch, capsys):
    home, _team = _pull_fixture(tmp_path)
    monkeypatch.setattr(sys, "argv", ["okfmem", "--store", home])
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(SystemExit) as e:
        mp.main()

    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "okfmem pull: " in out
    assert "okfmem pull [gone]: " in out and "not cloned" in out
    assert "okfmem pull [team]: fast-forwarded." in out


def test_pull_main_exits_one_on_an_attached_conflict(monkeypatch):
    ok = {"pulled": False, "up_to_date": True, "conflict": False,
          "offline": False, "reason": "already up to date."}
    clash = dict(ok, up_to_date=False, conflict=True, reason="conflict")
    monkeypatch.setattr(mp, "pull_store", lambda *a, **k: ok)
    monkeypatch.setattr(mp, "pull_attached", lambda *a, **k: [
        ({"name": "team", "path": "/t", "remote": None}, clash)])
    monkeypatch.setattr(sys, "argv", ["okfmem", "--quiet", "--store", "/whatever"])
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(SystemExit) as e:
        mp.main()

    assert e.value.code == 1


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@needs_git
def test_status_lists_every_attached_store_with_its_last_fetch(tmp_path, capsys):
    _origin, team = _seed_clone(tmp_path, "team", {"pages/t.md": "v1\n"})
    _git(team, "fetch", "-q")
    never = tmp_path / "never"
    _git(str(tmp_path), "clone", "-q", _origin, str(never))
    fetch_head = Path(never, ".git", "FETCH_HEAD")
    if fetch_head.exists():
        fetch_head.unlink()
    store = _make_store(tmp_path / "home", stores=[
        {"name": "team", "path": team},
        {"name": "never", "path": str(never)},
        {"name": "gone", "path": str(tmp_path / "gone"), "remote": "acme/gone"},
    ])

    mi.cmd_status(store)

    out = capsys.readouterr().out
    assert "attached stores (3):" in out
    team_line = next(ln for ln in out.splitlines() if ln.strip().startswith("team "))
    assert "last fetch: 20" in team_line and "UTC" in team_line
    never_line = next(ln for ln in out.splitlines() if ln.strip().startswith("never "))
    assert "last fetch: never" in never_line
    gone_line = next(ln for ln in out.splitlines() if ln.strip().startswith("gone "))
    assert "not cloned" in gone_line
    assert "git clone https://github.com/acme/gone.git" in gone_line


def test_status_without_attached_stores_prints_no_section(tmp_path, capsys):
    store = _make_store(tmp_path / "home")

    mi.cmd_status(store)

    assert "attached stores" not in capsys.readouterr().out
