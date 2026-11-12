"""okfmem inbox dump + apply (#165): dump every pending page for a judge outside
the engine, then apply its decisions.json in one validated, confirmed pass."""
import io
import json
import os
import shutil
import sys

import pytest

import memory_inbox as mx
from memory_frontmatter import find_frontmatter, merge_labels, parse_fields, parse_labels
from test_inbox import _git, _page, _push, _read, _remote_refs, _write, env  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")

TODAY = "2026-11-10"


def _dump(env, name="dump"):  # noqa: F811
    out_dir = os.path.join(env["tmp"], name)
    code, result = mx.dump(env["store"], out_dir, out=lambda _: None)
    assert code == 0, result
    with open(os.path.join(out_dir, mx.DUMP_FILE), encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    return out_dir, {r["slug"]: r for r in records}


def _decisions(out_dir, decisions):
    path = os.path.join(out_dir, "decisions.json")
    _write(path, json.dumps(decisions))
    return path


def _apply(env, path, confirm=lambda: True, do_sync=True):  # noqa: F811
    lines = []
    code = mx.apply(env["store"], path, confirm, out=lines.append, today=TODAY,
                    imported_by="tester", do_sync=do_sync)
    return code, "\n".join(lines)


def _store_state(env):  # noqa: F811
    store = env["store"]
    return (_git(store, "rev-parse", "HEAD"), _git(store, "status", "--porcelain"),
            _remote_refs(env["origin"]))


def _good_decisions(ids):
    return [
        {"id": ids["existing"]["id"], "action": "merge", "target": "existing",
         "add": "More detail.",
         "replace": [{"old": "Body text.", "new": "Body text, corrected."}],
         "confidence": "high", "reason": "same rule"},
        {"id": ids["fresh-lesson"]["id"], "action": "new",
         "page": {"slug": "runner-image", "name": "Runner image",
                  "description": "Pin the runner image", "type": "feedback"}},
        {"id": ids["leak-gate"]["id"], "action": "drop", "reason": "noise"},
    ]


# --------------------------------------------------------------------------
# dump
# --------------------------------------------------------------------------

def test_dump_writes_records_and_leaves_store_and_refs_alone(env, tmp_path):
    _push(env)
    store = env["store"]
    mx.fetch_inbox(store)
    before = _store_state(env)
    local = _git(store, "for-each-ref", "--format=%(refname) %(objectname)", mx.INBOX_NS)

    _, recs = _dump(env)

    assert _store_state(env) == before
    assert _git(store, "for-each-ref", "--format=%(refname) %(objectname)",
                mx.INBOX_NS) == local
    assert sorted(r["id"] for r in recs.values()) == ["p1", "p2", "p3"]
    ex = recs["existing"]
    assert ex["ref"] == _remote_refs(env["origin"]) and len(ex["sha"]) == 40
    assert ex["project"] == "demo" and ex["meta"]["repo"] == "acme/demo"
    assert ex["text"].startswith("---\nname: Existing rule v2")
    assert {"slug": "existing", "why": "slug",
            "description": "Always run the leak gate before committing"} in ex["near"]
    assert [n["slug"] for n in recs["leak-gate"]["near"]] == ["existing"]
    assert recs["leak-gate"]["near"][0]["why"] == "name"
    assert recs["fresh-lesson"]["near"] == [] and recs["fresh-lesson"]["leaks"] == []
    assert os.path.expanduser("~").startswith(str(tmp_path))  # isolated HOME


def test_dump_refuses_an_out_dir_inside_the_store(env):
    _push(env)
    out_dir = os.path.join(env["store"], "dump")
    code, result = mx.dump(env["store"], out_dir, out=lambda _: None)
    assert code == 2 and "inside the store" in result["error"]
    assert not os.path.exists(out_dir)


def test_dump_json_cli_prints_one_object(env, monkeypatch, capsys):
    _push(env)
    out_dir = os.path.join(env["tmp"], "dump")
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "dump", "--store", env["store"],
                                      "--out", out_dir, "--json"])
    with pytest.raises(SystemExit) as e:
        mx.main()
    assert e.value.code == 0
    got = json.loads(capsys.readouterr().out)
    assert got == {"out": os.path.join(out_dir, mx.DUMP_FILE), "pages": 3, "pushes": 1}


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def test_apply_writes_pages_clears_refs_and_a_rerun_is_a_no_op(env):
    _push(env)
    out_dir, recs = _dump(env)
    path = _decisions(out_dir, _good_decisions(recs))

    code, text = _apply(env, path)

    assert code == 0, text
    assert "1 drop / 1 merge into 1 page(s) / 1 new" in text
    proj = os.path.join(env["store"], "projects", "demo")
    new = _read(os.path.join(proj, "runner-image.md"))
    fields = parse_fields(new, find_frontmatter(new))
    assert fields["name"] == "Runner image" and fields["imported_by"] == "tester"
    assert fields["source"] == "gaal" and "Body text." in new
    assert "- [Runner image](runner-image.md) — Pin the runner image" in _read(
        os.path.join(proj, "MEMORY.md"))
    merged = _read(os.path.join(proj, "existing.md"))
    assert "Body text, corrected." in merged and "Body text.\n" not in merged
    assert merged.endswith(f"## From inbox ({TODAY})\n\nMore detail.\n")
    assert not os.path.exists(os.path.join(proj, "leak-gate.md"))
    assert _remote_refs(env["origin"]) == ""
    state = _store_state(env)
    assert state[1] == ""  # ended with okfmem sync

    code, text = _apply(env, path)

    assert code == 0, text
    assert "nothing to do" in text
    assert _store_state(env) == state
    assert _read(os.path.join(proj, "existing.md")) == merged
    assert _read(os.path.join(proj, "runner-image.md")) == new


def _uncovered(recs):
    return _good_decisions(recs)[:2], "not covered by any decision: " + recs["leak-gate"]["id"]


def _double_cover(recs):
    d = _good_decisions(recs)
    d[2]["covers"] = [recs["fresh-lesson"]["id"]]
    return d, f"{recs['fresh-lesson']['id']} is already covered by"


def _missing_target(recs):
    d = _good_decisions(recs)
    d[0]["target"] = "nope"
    return d, "merge target 'nope' is neither a page of demo"


def _existing_new_slug(recs):
    d = _good_decisions(recs)
    d[1]["page"]["slug"] = "existing"
    return d, "new slug 'existing' already exists"


def _replace_missing(recs):
    d = _good_decisions(recs)
    d[0]["replace"] = [{"old": "No such line", "new": "A corrected line."}]
    return d, "replace: 'No such line' not found in existing"


def _leaky_add(recs):
    d = _good_decisions(recs)
    d[0]["add"] = "-----BEGIN RSA PRIVATE KEY-----"
    return d, "leak gate: private key"


def _unknown_new_id(recs):
    d = _good_decisions(recs)
    d[1]["id"] = "p99"
    return d, "unknown id 'p99'"


@pytest.mark.parametrize("make", [_uncovered, _double_cover, _missing_target,
                                  _existing_new_slug, _replace_missing, _leaky_add,
                                  _unknown_new_id])
def test_apply_refuses_a_bad_file_and_writes_nothing(env, make):
    _push(env)
    out_dir, recs = _dump(env)
    decisions, problem = make(recs)
    path = _decisions(out_dir, decisions)
    existing = _read(os.path.join(env["store"], "projects", "demo", "existing.md"))
    before = _store_state(env)

    code, text = _apply(env, path, confirm=lambda: pytest.fail("asked to confirm"))

    assert code == 1
    assert "nothing written" in text and problem in text
    assert _store_state(env) == before
    assert _read(os.path.join(env["store"], "projects", "demo", "existing.md")) == existing


def test_apply_lists_every_problem_at_once(env):
    _push(env)
    out_dir, recs = _dump(env)
    d = _good_decisions(recs)
    d[0]["target"] = "nope"
    d[1]["page"]["slug"] = "existing"
    d[2]["id"] = "p99"
    code, text = _apply(env, _decisions(out_dir, d))
    assert code == 1
    assert "4 problem(s)" in text
    for part in ("unknown id 'p99'", "merge target 'nope'", "new slug 'existing'",
                 "not covered by any decision"):
        assert part in text


def test_ref_re_pushed_after_the_dump_is_left_pending(env, tmp_path):
    moved = _push(env)
    other = str(tmp_path / "desk-pages")
    _write(os.path.join(other, "desk.md"), _page("Desk", "A page from the desk"))
    cleared = _push(env, machine="desk", src=other)
    out_dir, recs = _dump(env)
    page = _page("Re-pushed", "Changed after the dump").encode("utf-8")
    sha = mx.build_commit(env["worker"], [("again.md", page)], {"source": "gaal"})
    _git(env["worker"], "push", "-q", "-f", "origin", f"{sha}:{moved}")

    decisions = [{"id": r["id"], "action": "drop"} for r in recs.values()]
    code, text = _apply(env, _decisions(out_dir, decisions))

    assert code == 0, text
    assert f"still pending: {moved}" in text
    assert _remote_refs(env["origin"]) == moved
    assert _git(env["origin"], "rev-parse", moved) == sha
    assert cleared not in _remote_refs(env["origin"])


def _two_page_push(env, tmp_path):  # noqa: F811
    src = str(tmp_path / "pair-pages")
    _write(os.path.join(src, "alpha.md"), _page("Alpha", "First of a pair", body="Alpha body.\n"))
    _write(os.path.join(src, "beta.md"), _page("Beta", "Second of a pair", body="Beta body.\n"))
    return _push(env, machine="desk", src=src)


def test_partial_decisions_write_the_decided_pages_and_keep_the_pending_refs(env, tmp_path):
    full = _push(env)
    pair = _two_page_push(env, tmp_path)
    pair_sha = _git(env["origin"], "rev-parse", pair)
    out_dir, recs = _dump(env)
    decisions = [*_good_decisions(recs),
                 {"id": recs["alpha"]["id"], "action": "new"},
                 {"id": recs["beta"]["id"], "action": "skip"}]

    code, text = _apply(env, _decisions(out_dir, decisions))

    assert code == 0, text
    assert "4 decided, 1 left pending" in text
    assert "clear 1 push(es); keep 1 holding a pending page" in text
    proj = os.path.join(env["store"], "projects", "demo")
    assert os.path.isfile(os.path.join(proj, "runner-image.md"))
    assert "Alpha body." in _read(os.path.join(proj, "alpha.md"))
    assert not os.path.exists(os.path.join(proj, "beta.md"))
    remote = _remote_refs(env["origin"]).splitlines()
    assert full not in remote and remote == [pair]
    assert _git(env["origin"], "rev-parse", pair) == pair_sha
    assert _store_state(env)[1] == ""


def test_a_two_page_push_is_cleared_only_once_both_pages_are_decided(env, tmp_path):
    pair = _two_page_push(env, tmp_path)
    out_dir, recs = _dump(env)
    code, text = _apply(env, _decisions(out_dir, [
        {"id": recs["alpha"]["id"], "action": "drop"},
        {"id": recs["beta"]["id"], "action": "skip"}]))
    assert code == 0 and "nothing to do — 1 decided, 1 left pending." in text, text
    assert _remote_refs(env["origin"]) == pair

    out_dir, recs = _dump(env, "dump2")
    code, text = _apply(env, _decisions(out_dir, [
        {"id": recs["alpha"]["id"], "action": "drop"},
        {"id": recs["beta"]["id"], "action": "new"}]))
    assert code == 0, text
    assert "2 decided, 0 left pending" in text
    assert _remote_refs(env["origin"]) == ""
    assert os.path.isfile(os.path.join(env["store"], "projects", "demo", "beta.md"))


def test_apply_with_every_page_skipped_writes_nothing_and_asks_nothing(env):
    ref = _push(env)
    out_dir, recs = _dump(env)
    before = _store_state(env)
    code, text = _apply(env, _decisions(out_dir, [{"id": r["id"], "action": "skip"}
                                                   for r in recs.values()]),
                        confirm=lambda: pytest.fail("asked to confirm"))
    assert code == 0 and "0 decided, 3 left pending" in text, text
    assert _store_state(env) == before and _remote_refs(env["origin"]) == ref


def test_a_skip_still_needs_a_known_id_covered_once(env):
    _push(env)
    out_dir, recs = _dump(env)
    d = _good_decisions(recs)
    d.append({"id": recs["leak-gate"]["id"], "action": "skip"})
    d.append({"id": "p99", "action": "skip"})
    code, text = _apply(env, _decisions(out_dir, d))
    assert code == 1 and "nothing written" in text
    assert f"{recs['leak-gate']['id']} is already covered by" in text
    assert "unknown id 'p99'" in text


def test_delete_refs_lease_keeps_a_ref_moved_after_it_was_read(env):
    ref = _push(env)
    old = _git(env["origin"], "rev-parse", ref)
    sha = mx.build_commit(env["worker"], [("x.md", _page("X", "Y").encode())], {})
    _git(env["worker"], "push", "-q", "-f", "origin", f"{sha}:{ref}")

    deleted, failed = mx.delete_refs(env["store"], {ref: old})

    assert deleted == [] and "stale info" in failed[ref]
    assert _git(env["origin"], "rev-parse", ref) == sha


def test_apply_without_terminal_or_yes_writes_nothing_and_prints_the_command(
        env, monkeypatch, capsys):
    _push(env)
    out_dir, recs = _dump(env)
    path = _decisions(out_dir, _good_decisions(recs))
    before = _store_state(env)
    monkeypatch.setattr(sys, "argv", ["memory_inbox.py", "apply", "--store", env["store"],
                                      path])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as e:
        mx.main()
    assert e.value.code == 2
    out = capsys.readouterr().out
    assert "nothing written" in out
    assert f"okfmem inbox apply --store {env['store']} {path} --yes" in out
    assert _store_state(env) == before
    assert not os.path.exists(os.path.join(env["store"], "projects", "demo",
                                           "runner-image.md"))


def test_apply_declined_at_the_prompt_writes_nothing(env):
    _push(env)
    out_dir, recs = _dump(env)
    before = _store_state(env)
    code, text = _apply(env, _decisions(out_dir, _good_decisions(recs)),
                        confirm=lambda: False)
    assert code == 1 and "not confirmed" in text
    assert _store_state(env) == before


def test_apply_in_label_layout_labels_new_pages_and_merges_into_them(env):
    store = env["store"]
    pages = os.path.join(store, "pages")
    project = os.path.join(store, "projects", "demo")
    reg_path = os.path.join(store, "registry.json")
    registry = json.loads(_read(reg_path))
    registry.update(layout="labels", aliases={"repo:demo": ["repo:demo-old"]})
    _write(reg_path, json.dumps(registry))
    _write(os.path.join(store, ".gitignore"), "views/\n")
    _write(os.path.join(pages, "existing.md"),
           merge_labels(_read(os.path.join(project, "existing.md")), ["repo:demo-old"]))
    shutil.rmtree(project)
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "label layout")
    _git(store, "push", "-q")
    _push(env)
    out_dir, recs = _dump(env)
    assert recs["existing"]["near"][0]["slug"] == "existing"
    decisions = _good_decisions(recs)
    decisions[1]["page"]["labels"] = ["topic:ci"]
    decisions[2] = {"id": recs["leak-gate"]["id"], "action": "merge",
                    "target": "runner-image", "add": "Folded in from a duplicate."}

    code, text = _apply(env, _decisions(out_dir, decisions))

    assert code == 0, text
    new = _read(os.path.join(pages, "runner-image.md"))
    assert parse_labels(new) == ["topic:ci", "repo:demo"]
    assert new.endswith(f"## From inbox ({TODAY})\n\nFolded in from a duplicate.\n")
    assert "Body text, corrected." in _read(os.path.join(pages, "existing.md"))
    assert not os.path.exists(project)
    view = _read(os.path.join(store, "views", "demo", "MEMORY.md"))
    assert "(pages/runner-image.md)" in view
    assert _git(store, "status", "--porcelain") == ""
    assert _remote_refs(env["origin"]) == ""

    code, text = _apply(env, os.path.join(out_dir, "decisions.json"))
    assert code == 0 and "nothing to do" in text, text
    assert _read(os.path.join(pages, "runner-image.md")) == new


def test_apply_keeps_crlf_endings_from_a_dump_dir_with_a_space(env):
    store = env["store"]
    target = os.path.join(store, "projects", "demo", "existing.md")
    _git(store, "config", "core.autocrlf", "false")
    _write(target, _read(target).replace("\n", "\r\n"))
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "crlf page")
    _push(env)
    out_dir, recs = _dump(env, name="inbox dump")
    decisions = _good_decisions(recs)
    decisions[0]["add"] = "Line one\nLine two"

    code, text = _apply(env, _decisions(out_dir, decisions))
    assert code == 0, text
    merged = _read(target)
    assert "\n" not in merged.replace("\r\n", "")
    assert "Body text, corrected.\r\n" in merged
    assert merged.endswith(f"## From inbox ({TODAY})\r\n\r\nLine one\r\nLine two\r\n")

    code, text = _apply(env, os.path.join(out_dir, "decisions.json"))
    assert code == 0 and "nothing to do" in text, text
    assert _read(target) == merged


def test_apply_hint_keeps_a_windows_path_as_one_argument():
    hint = mx.apply_hint(r"C:\Users\you\okfmem-store",
                         r"C:\Users\you\inbox dump\decisions.json", "team")
    assert hint == (r"okfmem inbox apply --store C:\Users\you\okfmem-store --into team "
                    r'"C:\Users\you\inbox dump\decisions.json" --yes')


def test_replace_whose_new_text_contains_the_old_applies_once():
    text = "keep the gate on\n"
    assert mx._replace_state(text, "the gate", "the leak gate") == "apply"
    once = text.replace("gate", "leak gate", 1)
    assert mx._replace_state(once, "gate", "leak gate") == "applied"
    assert mx._replace_state("gate gate", "gate", "x") == "ambiguous"


def test_dispatcher_usage_names_dump_and_apply():
    from conftest import ROOT
    usage = _read(os.path.join(ROOT, "okfmem"))
    assert "okfmem inbox dump" in usage and "okfmem inbox apply" in usage


# --------------------------------------------------------------------------
# okfmem-inbox skill + BRIEF.md (#166)
# --------------------------------------------------------------------------

def test_okfmem_inbox_brief_lives_in_repo_and_has_no_store_specific_paths():
    from conftest import ROOT
    brief_path = os.path.join(ROOT, "skills", "okfmem-inbox", "BRIEF.md")
    assert os.path.isfile(brief_path)
    brief = _read(brief_path)
    assert mx.leak_hits(brief) == []
    for forbidden in ("okfmem-store", "OKFMEM_STORE", "STORE_PATH", "/Users/",
                      "/home/", "C:\\", "~/"):
        assert forbidden not in brief
    for required in ("pending.jsonl", "decisions.json", "covers", "replace",
                     "confidence", '"high"', '"low"', "question", "merge",
                     "drop", "new", "okfmem inbox apply"):
        assert required in brief


def test_okfmem_inbox_skill_writes_only_through_inbox_apply():
    from conftest import ROOT
    skill_path = os.path.join(ROOT, "skills", "okfmem-inbox", "SKILL.md")
    assert os.path.isfile(skill_path)
    skill = _read(skill_path)
    fields = parse_fields(skill, find_frontmatter(skill))
    assert fields["name"] == "okfmem-inbox"
    for required in ("BRIEF.md", "okfmem inbox dump --store", "30",
                     "confidence", "okfmem inbox apply", "okfmem inbox review --web",
                     "Write", "decisions.json"):
        assert required in skill
    assert "write only through `okfmem inbox apply`" in skill.lower()
    assert "never write, edit, or delete files under `pages/`" in skill.lower()
    assert "refs/okfmem/inbox/*" in skill


def test_init_links_okfmem_inbox_into_each_supported_harness_and_update_adds_it(
        tmp_path, monkeypatch):
    from conftest import ROOT
    import memory_codex as mc
    import memory_init as mi
    import memory_update as mu_upd

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".gemini").mkdir(parents=True)
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.setattr(mi.shutil, "which",
                        lambda name: f"/bin/{name}" if name in ("codex", "agy") else None)
    monkeypatch.setattr(mc, "supported", lambda: True)

    store = tmp_path / "store"
    (store / "pages").mkdir(parents=True)
    (store / "layout.yml").write_text("layout: labels\n", encoding="utf-8")

    dirs = mi.skill_dirs()
    assert set(dirs) == {"claude_code", "codex", "codex_legacy", "antigravity"}

    engine_skills = os.path.join(ROOT, "skills")
    for dest in dirs.values():
        os.makedirs(dest, exist_ok=True)
        for prior in ("okfmem", "okfmem-curate", "okfmem-save"):
            os.symlink(os.path.join(engine_skills, prior), os.path.join(dest, prior),
                       target_is_directory=True)
    assert "skills missing" in mc.status(str(store))

    monkeypatch.setattr(mu_upd, "_ensure_git_repo", lambda: None)
    monkeypatch.setattr(mu_upd, "_short", lambda ref="HEAD": "abc1234")
    monkeypatch.setattr(mu_upd, "_engine_branch_state", lambda: ("main", "main"))
    monkeypatch.setattr(mu_upd, "_git",
                        lambda *a, **kw: mx.subprocess.CompletedProcess(a, 0, "", ""))

    real_run = mu_upd.subprocess.run
    init_calls = []

    def run_init(args, **kw):
        if args[0] == "git":
            return real_run(args, **kw)
        init_calls.append(args)
        actions = mi.link_skills(dry_run=False)
        for harness in dirs:
            assert (harness, "okfmem-inbox", "link") in actions
        return mx.subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(mu_upd.subprocess, "run", run_init)
    with pytest.raises(SystemExit) as exc:
        mu_upd.cmd_update(["--store", str(store)], yes=True)
    assert exc.value.code == 0
    assert len(init_calls) == 1 and init_calls[0][1].endswith("memory_init.py")

    for harness, dest in dirs.items():
        skill_dir = os.path.join(dest, "okfmem-inbox")
        assert os.path.isfile(os.path.join(skill_dir, "SKILL.md")), harness
        assert os.path.isfile(os.path.join(skill_dir, "BRIEF.md")), harness
    assert "skills available" in mc.status(str(store))
    second = mi.link_skills(dry_run=False)
    for harness in dirs:
        assert (harness, "okfmem-inbox", "ok") in second


def test_tier3_copy_repoints_when_brief_md_changes(tmp_path):
    from conftest import ROOT
    import memory_init as mi

    target = os.path.join(ROOT, "skills", "okfmem-inbox")
    copy_dir = str(tmp_path / "okfmem-inbox")
    shutil.copytree(target, copy_dir)
    _write(os.path.join(copy_dir, mi.MANAGED_COPY_MARKER), target + "\n")
    assert mi._link_matches_target(copy_dir, target) is True

    _write(os.path.join(copy_dir, "BRIEF.md"), "stale brief\n")
    assert mi._link_matches_target(copy_dir, target) is False


def test_mostly_duplicate_inbox_asks_no_more_questions_than_low_confidence_decisions(env):
    cand = env["cand"]
    for i in range(1, 4):
        _write(os.path.join(cand, f"dup-{i}.md"),
               _page(f"Existing rule dup {i}",
                     "Always run the leak gate before committing",
                     body="Run python3 scripts/check-leaks.py before every commit."))
    _write(os.path.join(cand, "narration.md"),
           _page("PR 42 merged", "Merged PR 42 after CI passed",
                 body="PR #42 merged at sha 1234567."))
    _write(os.path.join(cand, "ambiguous-pref.md"),
           _page(" Weekly digest cadence ", "Send digest on Fridays or Mondays",
                 typ="feedback",
                 body="Prefer sending the weekly digest on Friday afternoon."))
    _push(env)

    before = _store_state(env)
    out_dir, recs = _dump(env)
    assert _store_state(env) == before

    dup_ids = sorted(recs[k]["id"] for k in ("existing", "leak-gate", "dup-1", "dup-2", "dup-3"))
    drop_ids = sorted(recs[k]["id"] for k in ("fresh-lesson", "narration"))
    low_id = recs["ambiguous-pref"]["id"]

    decisions = [
        {"id": dup_ids[0], "covers": dup_ids[1:], "action": "merge",
         "target": "existing", "add": "Run python3 scripts/check-leaks.py before every commit.",
         "confidence": "high", "reason": "folds five duplicate leak-gate pages"},
        {"id": drop_ids[0], "covers": drop_ids[1:], "action": "drop",
         "confidence": "high", "reason": "run narration"},
        {"id": low_id, "action": "new",
         "page": {"slug": "digest-cadence", "name": "Weekly digest cadence",
                  "description": "Send the weekly digest on Friday afternoon",
                  "type": "feedback"},
         "confidence": "low",
         "question": "Keep Friday afternoon as a standing digest preference, or drop it?",
         "reason": "team preference needs human choice"},
    ]

    remote = mx.remote_inbox_refs(env["store"])
    plan, problems = mx.plan_apply(env["store"], list(recs.values()), decisions,
                                   remote, TODAY, "tester")
    assert problems == []
    assert len(plan["low"]) == 1
    assert low_id in plan["low"][0]

    decisions[-1]["confidence"] = "high"
    code, text = _apply(env, _decisions(out_dir, decisions))
    assert code == 0, text
    assert "low confidence" not in text
    assert "2 drop / 5 merge into 1 page(s) / 1 new" in text
    assert os.path.isfile(os.path.join(env["store"], "projects", "demo", "digest-cadence.md"))
    assert _remote_refs(env["origin"]) == ""
