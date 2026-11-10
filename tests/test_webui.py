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


def _run_review(env, work, confirm=lambda: True, decisions_post=None):
    srv = _shell()
    lines, result = [], {}
    t = threading.Thread(target=lambda: result.update(code=mx.review_web(
        env["store"], work, confirm, out=lines.append, imported_by="tester",
        open_browser=False, server=srv)))
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


def test_with_decisions_json_the_state_carries_them_and_marks_low_ones(env):
    _push(env)
    work, recs = _dump(env, "w2")
    ds = _good_decisions(recs)
    ds[1]["confidence"] = "low"
    _decisions(work, ds)
    state, _, _, _ = _run_review(env, work)
    assert state["has_decisions"]
    assert [d["id"] for d in state["decisions"] if d.get("confidence") == "low"] == [
        recs["fresh-lesson"]["id"]]


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
