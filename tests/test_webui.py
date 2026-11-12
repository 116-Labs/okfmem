"""okfmem inbox review --web (#167): the local page shell and the review page."""
import json
import os
import shutil
import threading
import urllib.error
import urllib.request

import pytest

import memory_inbox as mx
import memory_webui as wu
from test_inbox import _git, _push, _push_hand_built, env  # noqa: F401
from test_inbox_apply import _decisions, _dump, _good_decisions, _store_state

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


def _call(srv, path, token=True, body=None):
    url = f"http://127.0.0.1:{srv.port}{path}"
    headers = {wu.TOKEN_HEADER: srv.token} if token else {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _wake(srv):
    """Unblock a pending handle_request after stop(). serve may already have
    closed the socket (it polls every 0.5s), so a refused or reset connection
    is fine."""
    try:
        _call(srv, "/api/state")
    except (urllib.error.URLError, OSError):
        pass


def _serve(srv):
    t = threading.Thread(target=srv.serve, kwargs={"open_browser": False}, daemon=True)
    t.start()
    return t


def _shell(api=None):
    return wu.PageServer({"/": "inbox_review.html"}, api or {"/api/x": lambda p: (200, {"ok": 1})})


def test_server_binds_loopback_only():
    srv = _shell()
    try:
        assert srv.httpd.server_address[0] == "127.0.0.1" and srv.port > 0
    finally:
        srv.httpd.server_close()


def test_request_without_the_token_gets_403_including_post():
    srv = _shell()
    _serve(srv)
    try:
        assert _call(srv, "/", token=False)[0] == 403
        assert _call(srv, "/api/x", token=False, body={"a": 1})[0] == 403
        assert _call(srv, "/api/x", body={"a": 1})[0] == 200
        assert _call(srv, "/?t=wrong", token=False)[0] == 403
        assert _call(srv, f"/?t={srv.token}", token=False)[0] == 200
    finally:
        srv.stop()
        _wake(srv)


def test_idle_timeout_ends_serve():
    srv = wu.PageServer({}, {}, idle_timeout=0.2)
    assert srv.serve(open_browser=False) == "idle timeout"


def _run_review(env, work, confirm=lambda: True, decisions_post=None, lines=None,
                srv=None, during=None, **kw):
    srv = srv or _shell()
    lines, result = [] if lines is None else lines, {}
    t = threading.Thread(target=lambda: result.update(code=mx.review_web(
        env["store"], work, confirm, out=lines.append, imported_by="tester",
        open_browser=False, server=srv, **kw)))
    t.start()
    for _ in range(200):
        if "/api/state" in srv.api:
            break
        threading.Event().wait(0.05)
    state = json.loads(_call(srv, "/api/state")[1])
    if during is not None:
        result["during"] = during(srv, state)
    post = None
    if decisions_post is not None:
        post = _call(srv, "/api/done", body={"decisions": decisions_post(state)})
    if t.is_alive() and (post is None or post[0] != 200):
        srv.stop()
        _wake(srv)
    t.join(10)
    return state, post, result, lines


def test_without_decisions_the_page_lists_every_pending_page(env):
    _push(env)
    state, _, _, _ = _run_review(env, os.path.join(env["tmp"], "w1"))
    assert len(state["records"]) == 3 and not state["has_decisions"]
    assert state["decisions"] == []
    assert state["visible"] == [r["id"] for r in state["records"]]


def test_with_decisions_json_only_low_and_undecided_pages_are_visible_by_default(env):
    _push(env)
    work, recs = _dump(env, "w2")
    ds = _good_decisions(recs)
    ds[1]["confidence"] = "low"
    _decisions(work, ds)
    state, _, _, _ = _run_review(env, work)
    assert state["has_decisions"]
    assert state["visible"] == [recs["fresh-lesson"]["id"]]

    _decisions(work, [ds[1], ds[2]])
    state, _, _, _ = _run_review(env, work)
    assert sorted(state["visible"]) == sorted([recs["existing"]["id"],
                                               recs["fresh-lesson"]["id"]])


def _by_slug(state):
    return {r["slug"]: r for r in state["records"]}


def test_a_page_merge_with_the_candidate_body_appends_it_to_the_target(env):
    _push(env)

    def page_decisions(state):
        recs = _by_slug(state)
        ex = recs["existing"]
        merge = {"id": ex["id"], "action": "merge", "target": "existing",
                 "confidence": "high", "add": ex["body"]}
        return [merge, *_good_decisions(recs)[1:]]

    state, post, result, _ = _run_review(env, os.path.join(env["tmp"], "w5"),
                                         decisions_post=page_decisions)
    assert _by_slug(state)["existing"]["body"] == "More detail."
    assert post[0] == 200 and result["code"] == 0
    with open(os.path.join(env["store"], "projects", "demo", "existing.md"),
              encoding="utf-8") as f:
        assert "More detail." in f.read()


def test_the_plan_is_on_the_terminal_before_the_confirm_prompt(env):
    _push(env)
    lines, seen = [], []

    def confirm():
        seen.extend(lines)
        return True

    _, post, _, _ = _run_review(env, os.path.join(env["tmp"], "w6"), confirm=confirm,
                                lines=lines,
                                decisions_post=lambda s: _good_decisions(_by_slug(s)))
    assert post[0] == 200
    assert any("1 drop / 1 merge" in line for line in seen)
    assert any("inbox: clear 1 push(es)" in line for line in seen)


def test_the_no_terminal_hint_keeps_into(env):
    _push(env)
    before = _store_state(env)
    _, post, _, _ = _run_review(env, os.path.join(env["tmp"], "w7"), confirm=lambda: None,
                                into="team",
                                decisions_post=lambda s: _good_decisions(_by_slug(s)))
    assert post[0] == 400 and b"--into team" in post[1] and b"--yes" in post[1]
    assert _store_state(env) == before


def test_a_prefilled_decisions_json_keeps_its_dump_when_the_inbox_changed(env):
    _push(env)
    work, recs = _dump(env, "w8")
    _decisions(work, _good_decisions(recs))
    other = os.path.join(env["tmp"], "later-pages")
    os.makedirs(other)
    with open(os.path.join(other, "later.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: Later\ndescription: Arrived after the dump\ntype: feedback\n"
                "---\n\nLater body.\n")
    _push(env, machine="aa01", src=other)

    state, _, _, _ = _run_review(env, work)
    assert {(r["id"], r["slug"], r["ref"]) for r in state["records"]} == {
        (r["id"], r["slug"], r["ref"]) for r in recs.values()}


def test_a_decisions_json_without_its_dump_is_refused(env):
    _push(env)
    work = os.path.join(env["tmp"], "w9")
    _decisions(work, [{"id": "p1", "action": "drop"}])
    lines = []
    code = mx.review_web(env["store"], work, lambda: True, out=lines.append,
                         open_browser=False)
    assert code == 2 and "has no pending.jsonl beside it" in "\n".join(lines)
    assert not os.path.exists(os.path.join(work, mx.DUMP_FILE))


def test_done_writes_what_apply_writes_on_the_same_decisions(env, tmp_path):
    _push(env)
    work, recs = _dump(env, "w3")
    ds = _good_decisions(recs)
    _, post, result, _ = _run_review(env, work, decisions_post=lambda s: ds)
    assert post[0] == 200 and result["code"] == 0
    assert os.path.exists(mx.new_page_path(env["store"], "demo", "runner-image"))
    assert not _store_state(env)[1]  # apply's sync committed everything it wrote
    with open(os.path.join(work, "decisions.json"), encoding="utf-8") as f:
        assert json.load(f) == ds


def test_done_with_a_page_left_pending_applies_the_rest_and_keeps_its_push(env):
    ref = _push(env)
    sha = _git(env["origin"], "rev-parse", ref)

    def leave_one_pending(state):
        recs = _by_slug(state)
        return [*_good_decisions(recs)[:2], {"id": recs["leak-gate"]["id"], "action": "skip"}]

    work = os.path.join(env["tmp"], "w10")
    _, post, result, _ = _run_review(env, work, decisions_post=leave_one_pending)
    assert post[0] == 200 and result["code"] == 0, post
    assert b"2 decided, 1 left pending" in post[1]
    assert os.path.exists(mx.new_page_path(env["store"], "demo", "runner-image"))
    assert _git(env["origin"], "rev-parse", ref) != sha

    state, _, _, _ = _run_review(env, work)
    assert state["has_decisions"]
    skipped = _by_slug(state)["leak-gate"]["id"]
    assert skipped in state["visible"]
    assert all(d.get("action") != "skip" for d in state["decisions"])


def test_skip_decisions_read_as_undecided_on_the_page():
    records = [{"id": f"p{i}", "project": "proj", "text": "---\nname: x\n---\n\nBody.\n"} for i in (1, 2, 3)]
    state = mx.web_state(records, [{"id": "p1", "action": "skip", "covers": ["p2"]},
                                   {"id": "p3", "action": "skip"}])
    assert state["decisions"] == [] and not state["has_decisions"]
    assert state["covered"] == [] and state["visible"] == ["p1", "p2", "p3"]
    state = mx.web_state(records, [{"id": "p1", "action": "drop"},
                                   {"id": "p2", "action": "skip"},
                                   {"id": "p3", "action": "skip"}])
    assert state["has_decisions"] and state["visible"] == ["p2", "p3"]


def test_review_page_leaves_undecided_pages_pending_and_pages_by_25():
    from conftest import ROOT
    with open(os.path.join(ROOT, "webui", "inbox_review.html"), encoding="utf-8") as f:
        html = f.read()
    assert "const PAGE_SIZE = 25;" in html
    for needle in ('"leave pending"', 'id="prev"', 'id="next"', "decided ·", "pending ·",
                   "total", 'action: "skip"', 'd.action || "skip"'):
        assert needle in html, needle


def test_done_with_an_undecided_page_is_refused_and_writes_nothing(env):
    _push(env)
    work, recs = _dump(env, "w4")
    before = _store_state(env)
    _, post, _, _ = _run_review(env, work, decisions_post=lambda s: [
        {"id": recs["existing"]["id"], "action": "drop"}])
    assert post[0] == 400 and b"no decision yet" in post[1]
    assert _store_state(env) == before


def _push_no_project(env):
    _push_hand_built(env, {"source": "x", "machine": "evil"}, "evil/1")


def test_state_reports_each_records_project_and_slug_collisions(env):
    _push(env)
    _push_no_project(env)
    work, recs = _dump(env, "s1")
    state = mx.web_state(mx.load_dump(os.path.join(work, mx.DUMP_FILE)), [], env["store"])
    got = {r["slug"]: (r["has_project"], r["collides"]) for r in state["records"]}
    assert got == {"existing": (True, ["existing"]), "fresh-lesson": (True, []),
                   "leak-gate": (True, []), "hand": (False, [])}


def _bad_new_decisions(recs):
    ds = _good_decisions(recs)
    ds[1]["page"]["slug"] = "existing"
    return ds + [{"id": recs["hand"]["id"], "action": "new"}]


def test_check_refuses_new_without_a_project_and_a_colliding_slug_and_writes_nothing(env):
    _push(env)
    _push_no_project(env)
    work, recs = _dump(env, "s2")
    before = _store_state(env)

    def during(srv, _):
        bad = json.loads(_call(srv, "/api/check",
                               body={"decisions": _bad_new_decisions(recs)})[1])
        good = _good_decisions(recs) + [{"id": recs["hand"]["id"], "action": "drop"}]
        return bad, json.loads(_call(srv, "/api/check", body={"decisions": good})[1])

    _, _, result, _ = _run_review(env, work, during=during)
    bad, good = result["during"]
    assert bad["by_id"][recs["fresh-lesson"]["id"]] == [
        "new slug 'existing' already exists in the store"]
    assert "resolve to no project" in bad["by_id"][recs["hand"]["id"]][0]
    assert len(bad["problems"]) == 2
    assert good["problems"] == [] and good["by_id"] == {}
    assert good["summary"] == {"decided": 4, "drop": 2, "merge": 1, "new": 1}
    assert _store_state(env) == before


def test_done_refused_answers_the_problems_and_never_reaches_the_confirm(env):
    _push(env)
    work, recs = _dump(env, "s3")
    asked, before = [], _store_state(env)
    bad = _good_decisions(recs)
    bad[1]["page"]["slug"] = "existing"
    srv = _shell()
    _, post, _, _ = _run_review(env, work, confirm=lambda: asked.append(1), srv=srv,
                                decisions_post=lambda s: bad)
    body = json.loads(post[1])
    assert post[0] == 400 and not asked
    assert body["problems"] == [
        f"decision 2 ({recs['fresh-lesson']['id']}): new slug 'existing' already exists "
        "in the store"]
    assert _store_state(env) == before


def test_done_accepted_says_confirm_only_while_the_terminal_asks_and_lists_the_writes(env):
    ref = _push(env)
    work, recs = _dump(env, "s4")
    srv, phases = _shell(), []

    def status():
        return json.loads(_call(srv, "/api/status")[1])["phase"]

    def confirm():
        phases.append(status())
        return True

    _, post, result, _ = _run_review(env, work, confirm=confirm, srv=srv,
                                     during=lambda s, _: status(),
                                     decisions_post=lambda s: _good_decisions(recs))
    body = json.loads(post[1])
    assert result["during"] == "idle" and phases == ["confirm"]
    assert post[0] == 200 and result["code"] == 0
    assert sorted(body["written"]) == [os.path.join("projects", "demo", "existing.md"),
                                       os.path.join("projects", "demo", "runner-image.md")]
    assert body["cleared"] == [ref]


def test_state_summarises_a_fully_decided_inbox_the_default_view_hides(env):
    _push(env)
    work, recs = _dump(env, "s5")
    _decisions(work, _good_decisions(recs))
    state, _, _, _ = _run_review(env, work)
    assert state["visible"] == []
    assert state["summary"] == {"decided": 3, "drop": 1, "merge": 1, "new": 1}


def test_decision_summary_counts_skip_as_pending():
    records = [{"id": f"p{i}", "project": "proj", "text": "x"} for i in (1, 2, 3)]
    summary = mx.decision_summary(records, [{"id": "p1", "action": "drop"},
                                            {"id": "p2", "action": "skip", "covers": ["p3"]}])
    assert summary == {"decided": 1, "drop": 1, "merge": 0, "new": 0}


def test_review_page_checks_and_submits_the_same_payload_with_skips():
    from conftest import ROOT
    with open(os.path.join(ROOT, "webui", "inbox_review.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'api("/api/check", {decisions: payload()})' in html
    assert 'api("/api/done", {decisions: payload()})' in html
