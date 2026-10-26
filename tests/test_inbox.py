"""okfmem inbox (#84): push candidate pages to `refs/okfmem/inbox/*` on the
store remote, list them, and review them into the store. Real repos against a
local bare remote — no network."""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

import memory_inbox as mx
import memory_pull as mp
import memory_sync as ms
from conftest import ROOT

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def _config(repo):
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _page(name, desc, body="Body text.\n", typ="feedback"):
    return f"---\nname: {name}\ndescription: {desc}\ntype: {typ}\n---\n\n{body}"


def _clone(origin, dest):
    _git(os.path.dirname(dest), "clone", "-q", origin, dest)
    _config(dest)
    return dest


@pytest.fixture
def env(tmp_path):
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    store = _clone(origin, str(tmp_path / "store"))
    proj = os.path.join(store, "projects", "demo")
    _write(os.path.join(proj, "MEMORY.md"),
           "# MEMORY — demo\n\n- [Existing rule](existing.md) — an existing page\n")
    _write(os.path.join(proj, "STATE.md"), "---\nname: demo-state\n---\n")
    _write(os.path.join(proj, "existing.md"),
           _page("Existing rule", "Always run the leak gate before committing"))
    _write(os.path.join(store, "registry.json"),
           json.dumps({"map": {os.path.join(str(tmp_path), "nowhere", "demo"): "demo"},
                       "overrides": {}}))
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "seed")
    _git(store, "push", "-q", "-u", "origin", "HEAD")
    worker = _clone(origin, str(tmp_path / "worker"))
    cand = str(tmp_path / "run-pages")
    _write(os.path.join(cand, "fresh-lesson.md"),
           _page("Fresh lesson", "Pin the runner image to avoid drift"))
    _write(os.path.join(cand, "existing.md"),
           _page("Existing rule v2", "Run the leak gate first", body="More detail.\n"))
    _write(os.path.join(cand, "leak-gate.md"),
           _page("Existing rule!", "Something else entirely here"))
    _write(os.path.join(cand, "notes.md"), "no frontmatter at all\n")
    return {"tmp": str(tmp_path), "origin": origin, "store": store,
            "worker": worker, "cand": cand}


def _remote_refs(origin):
    return _git(origin, "for-each-ref", "--format=%(refname)", "refs/okfmem")


def _push(env, out=None, machine="ak47", src=None):
    lines = [] if out is None else out
    code, ref = mx.push(env["worker"], src or env["cand"], "gaal", repo="acme/demo",
                        ref="#12", machine=machine, out=lines.append)
    assert code == 0, lines
    return ref


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------

def test_push_puts_pages_and_meta_under_inbox_ref(env):
    worker = env["worker"]
    head = _git(worker, "rev-parse", "HEAD")
    branches = _git(worker, "for-each-ref", "--format=%(refname)", "refs/heads")
    lines = []
    ref = _push(env, lines)

    assert ref.startswith("refs/okfmem/inbox/ak47/")
    assert _remote_refs(env["origin"]) == ref
    files = _git(env["origin"], "ls-tree", "--name-only", ref).splitlines()
    assert files == ["existing.md", "fresh-lesson.md", "leak-gate.md", "meta.json"]
    meta = json.loads(_git(env["origin"], "show", f"{ref}:meta.json"))
    assert meta["source"] == "gaal" and meta["repo"] == "acme/demo"
    assert meta["ref"] == "#12" and meta["machine"] == "ak47" and meta["time"]
    assert _git(env["origin"], "rev-list", "--count", ref) == "1"  # parentless
    assert any("skip notes.md" in ln for ln in lines)

    assert _git(worker, "rev-parse", "HEAD") == head
    assert _git(worker, "for-each-ref", "--format=%(refname)", "refs/heads") == branches
    assert _git(worker, "for-each-ref", "--format=%(refname)", "refs/okfmem") == ""
    assert _git(worker, "status", "--porcelain") == ""


def test_second_push_of_same_pages_creates_nothing(env):
    ref = _push(env)
    before = _git(env["origin"], "rev-parse", ref)
    lines = []
    assert _push(env, lines) == ref
    assert any("already in the inbox" in ln for ln in lines)
    assert _remote_refs(env["origin"]) == ref
    assert _git(env["origin"], "rev-parse", ref) == before


def test_push_with_no_valid_pages_pushes_nothing(env, tmp_path):
    empty = str(tmp_path / "bad")
    _write(os.path.join(empty, "x.md"), "nope\n")
    code, ref = mx.push(env["worker"], empty, "gaal", out=lambda _: None)
    assert code == 0 and ref is None
    assert _remote_refs(env["origin"]) == ""


# --------------------------------------------------------------------------
# never automatic
# --------------------------------------------------------------------------

def test_pull_sync_and_consolidate_never_fetch_inbox_refs(env):
    _push(env)
    store = env["store"]
    _write(os.path.join(store, "projects", "demo", "STATE.md"), "---\nname: x\n---\nnew\n")
    assert mp.pull_store(store)["offline"] is False
    assert ms.sync_store(store, "local edit")["pushed"] is True
    assert _git(store, "for-each-ref", "--format=%(refname)", "refs/okfmem") == ""
    assert not os.path.exists(os.path.join(store, "projects", "demo", "fresh-lesson.md"))
    # consolidate (the Stop hook) reaches the remote only through sync_store.
    for mod in ("memory_pull.py", "memory_sync.py", "memory_consolidate.py"):
        assert "okfmem/inbox" not in _read(os.path.join(ROOT, mod)), mod


# --------------------------------------------------------------------------
# inbox pull
# --------------------------------------------------------------------------

def test_inbox_pull_lists_pending_pages_with_origin(env):
    _push(env)
    lines = []
    assert mx.pull(env["store"], out=lines.append) == 0
    text = "\n".join(lines)
    assert "3 pending page(s) in 1 push(es)" in text
    assert "demo:" in text
    for slug in ("existing", "fresh-lesson", "leak-gate"):
        assert f"  {slug}  (source=gaal repo=acme/demo ref=#12 machine=ak47)" in text


def test_resolve_repo_project_prefers_registered_origin(env, tmp_path):
    checkout = str(tmp_path / "checkout")
    os.makedirs(checkout)
    _git(checkout, "init", "-q")
    _git(checkout, "remote", "add", "origin", "https://github.com/Acme/widget.git")
    _write(os.path.join(env["store"], "registry.json"),
           json.dumps({"map": {checkout: "wproj"}, "overrides": {}}))
    assert mx.resolve_repo_project(env["store"], "acme/widget") == "wproj"
    assert mx.resolve_repo_project(env["store"], "acme/demo") == "demo"
    assert mx.resolve_repo_project(env["store"], "acme/unknown") is None


# --------------------------------------------------------------------------
# inbox review
# --------------------------------------------------------------------------

def test_review_flags_accepts_merges_drops_and_clears_ref(env):
    _push(env)
    store = env["store"]
    choices = {"fresh-lesson": ("accept", None), "existing": ("merge", "existing"),
               "leak-gate": ("drop", None)}
    seen = {}

    def decide(push_, page, flags):
        seen[page["slug"]] = flags
        return choices[page["slug"]]

    counts = mx.review(store, decide, lambda repo: pytest.fail("asked"),
                       out=lambda _: None, today="2026-10-19")

    assert counts == {"accepted": 1, "merged": 1, "dropped": 1, "deleted": 1}
    assert any("slug matches existing page existing.md" in f for f in seen["existing"])
    assert any("name close to existing page existing" in f for f in seen["leak-gate"])
    assert not any("slug matches" in f for f in seen["fresh-lesson"])

    proj = os.path.join(store, "projects", "demo")
    assert "Pin the runner image" in _read(os.path.join(proj, "fresh-lesson.md"))
    assert ("- [Fresh lesson](fresh-lesson.md) — Pin the runner image to avoid drift"
            in _read(os.path.join(proj, "MEMORY.md")))
    merged = _read(os.path.join(proj, "existing.md"))
    assert "## Merged from inbox — 2026-10-19 (gaal@ak47, acme/demo #12)" in merged
    assert merged.rstrip().endswith("More detail.")
    assert not os.path.exists(os.path.join(proj, "leak-gate.md"))

    assert _remote_refs(env["origin"]) == ""
    assert _git(store, "for-each-ref", "--format=%(refname)", "refs/okfmem") == ""
    assert _git(store, "status", "--porcelain") == ""  # ended with okfmem sync
    assert _git(store, "rev-parse", "HEAD") == _git(store, "rev-parse", "@{u}")


def test_review_skip_keeps_only_unhandled_pages_pending(env):
    ref = _push(env)
    store = env["store"]
    mx.review(store, lambda p, page, f: ("skip", None) if page["slug"] == "leak-gate"
              else ("drop", None), lambda repo: None, out=lambda _: None,
              do_sync=False)
    remaining = _git(env["origin"], "ls-tree", "--name-only", ref).splitlines()
    assert remaining == ["leak-gate.md", "meta.json"]
    lines = []
    mx.pull(store, out=lines.append)
    assert "1 pending page(s)" in "\n".join(lines)


def test_review_asks_for_project_when_repo_unresolved(env, tmp_path):
    other = str(tmp_path / "other-pages")
    _write(os.path.join(other, "solo.md"), _page("Solo", "A lone page"))
    code, _ = mx.push(env["worker"], other, "gaal", repo="acme/elsewhere",
                      machine="desk", out=lambda _: None)
    assert code == 0
    asked = []

    def ask(repo):
        asked.append(repo)
        return "fresh-proj"

    mx.review(env["store"], lambda p, page, f: ("accept", None), ask,
              out=lambda _: None, do_sync=False)
    assert asked == ["acme/elsewhere"]
    proj = os.path.join(env["store"], "projects", "fresh-proj")
    assert os.path.isfile(os.path.join(proj, "solo.md"))
    assert "(solo.md)" in _read(os.path.join(proj, "MEMORY.md"))


def test_pending_ignores_index_and_state_names_from_a_hand_built_ref(env):
    worker = env["worker"]
    page = _page("Hostile", "Overwrite the index").encode("utf-8")
    sha = mx.build_commit(worker, [("MEMORY.md", page), ("STATE.md", page),
                                   ("ok-page.md", page)],
                          {"source": "x", "machine": "evil"})
    _git(worker, "push", "-q", "origin", f"{sha}:refs/okfmem/inbox/evil/1")
    mx.fetch_inbox(env["store"])
    [push_] = mx.pending(env["store"])
    assert [p["name"] for p in push_["pages"]] == ["ok-page.md"]


def _push_hand_built(env, meta, ref_name):
    page = _page("Hand built", "A hand built page").encode("utf-8")
    sha = mx.build_commit(env["worker"], [("hand.md", page)], meta)
    _git(env["worker"], "push", "-q", "origin", f"{sha}:refs/okfmem/inbox/{ref_name}")


def test_pending_drops_non_string_meta_values_and_still_lists_other_refs(env):
    _push(env)
    _push_hand_built(env, {"source": "x", "machine": "evil", "repo": 5, "ref": ["a"]},
                     "evil/1")
    mx.fetch_inbox(env["store"])
    pushes = mx.pending(env["store"])
    assert len(pushes) == 2
    bad = next(p for p in pushes if p["meta"].get("machine") == "evil")
    assert "repo" not in bad["meta"] and "ref" not in bad["meta"]
    assert bad["project"] is None
    assert mx.render_pending(pushes)


def test_partial_review_of_a_ref_with_non_string_source_shrinks_it(env):
    pages = [("keep.md", _page("Keep", "Accept this page").encode("utf-8")),
             ("later.md", _page("Later", "Skip this page").encode("utf-8"))]
    sha = mx.build_commit(env["worker"], pages,
                          {"source": 5, "machine": None, "repo": "acme/demo"})
    _git(env["worker"], "push", "-q", "origin", f"{sha}:refs/okfmem/inbox/evil/3")

    mx.review(env["store"], lambda p, page, f: ("accept", None) if page["slug"] == "keep"
              else ("skip", None), lambda repo: None, out=lambda _: None, do_sync=False)

    remaining = _git(env["origin"], "ls-tree", "--name-only", "refs/okfmem/inbox/evil/3")
    assert remaining.splitlines() == ["later.md", "meta.json"]
    assert os.path.isfile(os.path.join(env["store"], "projects", "demo", "keep.md"))


def test_render_pending_escapes_control_characters_in_meta(env):
    _push_hand_built(env, {"source": "\x1b[2Kfake row", "machine": "m\x1b[2K",
                           "repo": "\x1b[2Kfake/repo", "ref": "r\x1b[2K"}, "evil/2")
    mx.fetch_inbox(env["store"])
    text = "\n".join(mx.render_pending(mx.pending(env["store"])))
    assert "\x1b" not in text
    assert "?[2Kfake/repo" in text and "source=?[2Kfake row" in text


def test_interactive_project_prompt_escapes_control_characters(monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p: prompts.append(p) or "")
    mx._interactive_project("\x1b[2Kfake/repo")
    assert "\x1b" not in prompts[0] and "?[2Kfake/repo" in prompts[0]


def test_importable_name_refuses_case_variants_of_index_and_state():
    for name in ("state.md", "State.md", "memory.md", "Memory-lane.md", "context.MD"):
        assert not mx.importable_name(name), name
    assert mx.importable_name("stateful-lesson.md")


def test_project_pages_skips_archive_year_subdirectories(tmp_path):
    proj = tmp_path / "projects" / "demo"
    _write(str(proj / "live.md"), _page("live", "a live page"))
    _write(str(proj / "archive" / "2026" / "gone.md"), _page("gone", "archived"))
    _write(str(proj / "archive" / "flat.md"), _page("flat", "archived"))
    assert set(mx.project_pages(str(tmp_path), "demo")) == {"live"}


def test_review_flags_near_duplicate_description(env, tmp_path):
    other = str(tmp_path / "desc-pages")
    _write(os.path.join(other, "gate-order.md"),
           _page("Totally different title", "Always run the leak gate before commits"))
    mx.push(env["worker"], other, "gaal", repo="acme/demo", machine="desk",
            out=lambda _: None)
    seen = {}

    def decide(push_, page, flags):
        seen[page["slug"]] = flags
        return ("skip", None)

    mx.review(env["store"], decide, lambda repo: None, out=lambda _: None, do_sync=False)
    assert "description close to existing page existing" in seen["gate-order"]
    assert not any(f.startswith("name close") for f in seen["gate-order"])


def test_review_shows_body_preview_with_control_characters_removed(env, tmp_path):
    other = str(tmp_path / "esc-pages")
    _write(os.path.join(other, "esc.md"),
           _page("Esc \x1b[2Jname", "Hidden", body="line one\n\x1b[8mhidden\x1b[0m\n"))
    mx.push(env["worker"], other, "gaal", repo="acme/demo", machine="desk",
            out=lambda _: None)
    lines = []
    mx.review(env["store"], lambda p, page, f: ("skip", None), lambda repo: None,
              out=lines.append, do_sync=False)
    text = "\n".join(lines)
    assert "\x1b" not in text
    assert "  | line one" in text and "  | ?[8mhidden?[0m" in text
    assert "  name: Esc ?[2Jname" in text


def test_review_interrupted_after_a_merge_does_not_offer_it_again(env):
    ref = _push(env)
    store = env["store"]

    def decide(push_, page, flags):
        if page["slug"] == "existing":
            return ("merge", "existing")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        mx.review(store, decide, lambda repo: None, out=lambda _: None, do_sync=False)
    remaining = _git(env["origin"], "ls-tree", "--name-only", ref).splitlines()
    assert remaining == ["fresh-lesson.md", "leak-gate.md", "meta.json"]

    offered = []
    mx.review(store, lambda p, page, f: offered.append(page["slug"]) or ("skip", None),
              lambda repo: None, out=lambda _: None, do_sync=False)
    assert offered == ["fresh-lesson", "leak-gate"]
    merged = _read(os.path.join(store, "projects", "demo", "existing.md"))
    assert merged.count("## Merged from inbox") == 1


def test_review_with_remote_unreachable_still_clears_handled_pages_locally(env, tmp_path):
    _push(env)
    store = env["store"]
    mx.fetch_inbox(store)
    _git(store, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    lines = []
    mx.review(store, lambda p, page, f: ("merge", "existing") if page["slug"] == "existing"
              else ("skip", None), lambda repo: None, out=lines.append, do_sync=False)
    assert any("could not update" in ln for ln in lines)
    [push_] = mx.pending(store)
    assert [p["slug"] for p in push_["pages"]] == ["fresh-lesson", "leak-gate"]


def test_merging_the_same_page_twice_appends_it_once(env):
    page = {"text": _page("X", "Y", body="Unique merged body.\n")}
    meta = {"source": "gaal", "machine": "ak47"}
    store = env["store"]
    mx.merge_page(store, "demo", "existing", page, meta, "2026-10-19")
    mx.merge_page(store, "demo", "existing", page, meta, "2026-10-20")
    merged = _read(os.path.join(store, "projects", "demo", "existing.md"))
    assert merged.count("Unique merged body.") == 1


def test_git_timeout_is_a_failed_run_not_a_traceback(env, monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(mx.subprocess, "run", hang)
    r = mx._git(env["store"], "fetch", "origin")
    assert r.returncode == 124 and b"timed out" in r.stderr


def test_review_refuses_a_dirty_store(env, monkeypatch, capsys):
    _push(env)
    store = env["store"]
    _write(os.path.join(store, "projects", "demo", "scratch.md"), "uncommitted\n")
    monkeypatch.setattr(sys, "argv", ["okfmem", "--store", store, "review"])
    monkeypatch.setattr(mx, "_isatty", lambda: True)
    monkeypatch.setattr(mx, "review", lambda *a, **k: pytest.fail("reviewed"))
    with pytest.raises(SystemExit) as e:
        mx.main()
    assert e.value.code == 3
    assert "working tree is dirty" in capsys.readouterr().out
    assert not os.path.exists(os.path.join(store, "projects", "demo", "fresh-lesson.md"))


def test_review_refuses_non_interactive(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["okfmem", "--store", env["store"], "review"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as e:
        mx.main()
    assert e.value.code == 2
    assert "interactive only" in capsys.readouterr().out


def test_dispatcher_routes_inbox():
    assert '"inbox": "memory_inbox.py"' in _read(os.path.join(ROOT, "okfmem"))


def _cli_push_json(env, src=None, store=None):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "okfmem"), "inbox", "push",
         src or env["cand"], "--source", "gaal", "--machine", "ak47", "--json",
         "--store", store or env["worker"]],
        capture_output=True, text=True)


def test_push_json_reports_created_then_noop(env):
    first = _cli_push_json(env)
    assert first.returncode == 0, first.stderr
    got = json.loads(first.stdout)
    assert got["created"] is True and got["pages"] == 3
    assert got["ref"] == _remote_refs(env["origin"])
    assert "skip notes.md" in first.stderr

    again = _cli_push_json(env)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == {"ref": got["ref"], "pages": 3, "created": False}


def test_push_json_with_no_valid_pages_has_null_ref(env, tmp_path):
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    r = _cli_push_json(env, src=empty)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"ref": None, "pages": 0, "created": False}


def test_push_json_failure_exits_nonzero_with_one_line_error(env, tmp_path):
    _git(env["worker"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    r = _cli_push_json(env)
    assert r.returncode != 0
    got = json.loads(r.stdout)
    assert list(got) == ["error"] and got["error"] and "\n" not in got["error"]


def test_push_clears_a_previous_error_on_success(env):
    result = {"error": "previous failure"}
    code, ref = mx.push(env["worker"], env["cand"], "gaal", machine="ak47",
                        out=lambda _: None, result=result)
    assert code == 0 and ref
    assert "error" not in result
    assert result["created"] is True and result["pages"] == 3


def test_push_json_reports_page_collection_oserror(env, monkeypatch, tmp_path, capsys):
    src = str(tmp_path / "pages")
    os.makedirs(src)
    monkeypatch.setattr(mx, "collect_pages",
                        lambda _: (_ for _ in ()).throw(PermissionError("cannot read pages")))
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "push", src,
                                       "--source", "gaal", "--json",
                                       "--store", env["worker"]])
    with pytest.raises(SystemExit) as exc:
        mx.main()
    captured = capsys.readouterr()
    assert exc.value.code != 0
    result = json.loads(captured.out)
    assert result == {"error": "cannot read pages"}
    assert "\n" not in result["error"]
    assert "cannot read pages" in captured.err


@pytest.mark.skipif(os.name != "nt", reason="PowerShell wrapper is Windows-only")
def test_powershell_wrapper_propagates_json_push_failure(env, tmp_path):
    _git(env["worker"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         os.path.join(ROOT, "okfmem.ps1"), "inbox", "push", env["cand"],
         "--source", "gaal", "--machine", "ak47", "--json", "--store",
         env["worker"]], capture_output=True, text=True)
    assert r.returncode != 0
    got = json.loads(r.stdout)
    assert list(got) == ["error"] and got["error"]


@pytest.mark.skipif(os.name != "nt", reason="cmd wrapper is Windows-only")
def test_cmd_wrapper_propagates_json_push_failure(env, tmp_path):
    _git(env["worker"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    r = subprocess.run(
        ["cmd", "/d", "/c", os.path.join(ROOT, "okfmem.cmd"),
         "inbox", "push", env["cand"], "--source", "gaal", "--machine", "ak47",
         "--json", "--store", env["worker"]], capture_output=True, text=True)
    assert r.returncode != 0
    got = json.loads(r.stdout)
    assert list(got) == ["error"] and got["error"]
