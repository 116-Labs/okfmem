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
from test_inbox import _push, env  # noqa: F401
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
        _call(srv, "/api/x")


def test_idle_timeout_ends_serve():
    srv = wu.PageServer({}, {}, idle_timeout=0.2)
    assert srv.serve(open_browser=False) == "idle timeout"


def _run_review(env, work, confirm=lambda: True, decisions_post=None, lines=None, **kw):
    srv = _shell()
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
    post = None
    if decisions_post is not None:
        post = _call(srv, "/api/done", body={"decisions": decisions_post(state)})
    if t.is_alive() and (post is None or post[0] != 200):
        srv.stop()
        _call(srv, "/api/state")
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


def test_done_with_an_undecided_page_is_refused_and_writes_nothing(env):
    _push(env)
    work, recs = _dump(env, "w4")
    before = _store_state(env)
    _, post, _, _ = _run_review(env, work, decisions_post=lambda s: [
        {"id": recs["existing"]["id"], "action": "drop"}])
    assert post[0] == 400 and b"no decision yet" in post[1]
    assert _store_state(env) == before
