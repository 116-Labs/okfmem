"""okfmem mcp (#147): stdio + Access-gated HTTP MCP server over the store.
Synthetic stores, a local bare remote, and a stubbed get-identity — no network."""

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ROOT
from plugins import memory_mcp as mm

TEAM, AUD, NOW = "acme", "aud-123", 1_000_000.0


def page(d, slug, labels=(), typ="feedback", body="watch the pool\n", desc="d"):
    fm = [f"name: {slug}", f"description: {desc}", f"type: {typ}"]
    if labels:
        fm.append(f"labels: [{', '.join(labels)}]")
    (d / f"{slug}.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8"
    )


@pytest.fixture
def store(tmp_path):
    (tmp_path / "pages").mkdir()
    page(tmp_path / "pages", "gaal-a", ["repo:gaal"], body="watch watch\n")
    page(tmp_path / "pages", "hive-b", ["repo:hive"])
    page(tmp_path / "pages", "me-c", ["repo:gaal"], typ="user")
    (tmp_path / "views" / "gaal").mkdir(parents=True)
    (tmp_path / "views" / "gaal" / "MEMORY.md").write_text("# gaal index\n")
    (tmp_path / "views" / "hive").mkdir(parents=True)
    (tmp_path / "views" / "hive" / "MEMORY.md").write_text("# hive index\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "gaal.md").write_text("gaal state\n")
    return str(tmp_path)


def call(store, scope, name, args, caller="local"):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    r = mm.handle_message(store, scope, msg, caller)["result"]
    return r["isError"], r["content"][0]["text"]


def remote(labels=("repo:gaal",), user=False):
    return mm.Scope(labels, allow_user=user)


def test_stdio_find_matches_cli_order(store):
    stdin = io.StringIO("\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "find", "arguments": {"terms": ["watch"]}}},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    ]) + "\n")
    out = io.StringIO()
    mm.serve_stdio(store, stdin, out)
    resps = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r["id"] for r in resps] == [1, 2, 3, 4]
    assert resps[0]["result"]["capabilities"] == {"tools": {}}
    assert {t["name"] for t in resps[1]["result"]["tools"]} == set(mm.TOOLS)
    got = json.loads(resps[2]["result"]["content"][0]["text"])
    cli = subprocess.run(
        [sys.executable, os.path.join(ROOT, "okfmem"), "find", "watch", "--json",
         "--store", store], capture_output=True, text=True, timeout=60)
    assert cli.returncode == 0, cli.stderr
    assert [r["path"] for r in got] == [r["path"] for r in json.loads(cli.stdout)]
    assert len(got) == 3
    assert resps[3]["error"]["code"] == -32601


def test_initialize_answers_a_supported_protocol_version():
    def init(version):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": version}}
        return mm.handle_message("", mm.LOCAL, msg)["result"]["protocolVersion"]
    assert init("2024-11-05") == "2024-11-05"
    assert init("1999-01-01") == mm.PROTOCOL_VERSIONS[0]


def test_tool_crash_is_an_internal_error_and_stdio_keeps_serving(store, monkeypatch):
    def boom(*a):
        raise RuntimeError("disk on fire")
    monkeypatch.setitem(mm.TOOLS, "find", (boom, "", {}, []))
    stdin = io.StringIO("\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "find", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]) + "\n")
    out = io.StringIO()
    mm.serve_stdio(store, stdin, out)
    first, second = [json.loads(x) for x in out.getvalue().splitlines()]
    assert first["error"] == {"code": -32603, "message": "internal error"}
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_malformed_messages_get_errors_not_a_crash(store):
    stdin = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": ["find"], "arguments": {}}}),
        "[" * 100_000,
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}),
    ]) + "\n")
    out = io.StringIO()
    mm.serve_stdio(store, stdin, out)
    codes = [json.loads(x).get("error", {}).get("code") for x in out.getvalue().splitlines()]
    assert codes == [-32602, -32700, -32700, None]


def test_stdio_has_no_read_scope(store):
    err, text = call(store, mm.LOCAL, "read_page", {"slug": "me-c"})
    assert not err and "type: user" in text


def test_remote_find_and_read_hide_out_of_scope_pages(store):
    scope = remote()
    _, text = call(store, scope, "find", {"terms": ["watch"]})
    assert [r["slug"] for r in json.loads(text)] == ["gaal-a"]
    hidden = call(store, scope, "read_page", {"slug": "hive-b"})
    missing = call(store, scope, "read_page", {"slug": "nothing"})
    assert hidden[0] and hidden[1].replace("hive-b", "X") == missing[1].replace("nothing", "X")
    assert call(store, scope, "read_page", {"slug": "me-c"})[0]
    assert not call(store, remote(user=True), "read_page", {"slug": "me-c"})[0]
    assert not call(store, remote(("*",)), "read_page", {"slug": "hive-b"})[0]


def test_remote_user_type_is_hidden_whatever_its_case_or_quoting(store, tmp_path):
    pages = tmp_path / "pages"
    page(pages, "me-d", ["repo:gaal"], typ='"User"')
    for scope in (remote(), remote(("*",))):
        assert call(store, scope, "read_page", {"slug": "me-d"})[0]
        _, text = call(store, scope, "find", {"terms": ["watch"]})
        assert "me-d" not in [r["slug"] for r in json.loads(text)]


def test_star_allows_every_label_but_not_an_unlabelled_page(store, tmp_path):
    page(tmp_path / "pages", "bare-e")
    assert call(store, remote(("*",)), "read_page", {"slug": "bare-e"})[0]
    assert not call(store, mm.LOCAL, "read_page", {"slug": "bare-e"})[0]


def test_read_page_never_serves_the_reserved_index_names(store, tmp_path):
    (tmp_path / "pages" / "MEMORY.md").write_text("---\nname: m\ndescription: d\n"
                                                  "type: feedback\n---\n")
    for slug in ("MEMORY", "memory"):
        assert call(store, mm.LOCAL, "read_page", {"slug": slug})[0]


def test_read_page_rejects_path_traversal(store):
    assert call(store, mm.LOCAL, "read_page", {"slug": "../views/gaal/MEMORY"})[0]


def test_remote_index_state_scope(store):
    scope = remote()
    assert not call(store, scope, "read_index", {"repo": "gaal"})[0]
    assert not call(store, scope, "read_state", {"repo": "gaal"})[0]
    assert call(store, scope, "read_index", {"repo": "hive"})[0]
    assert call(store, scope, "read_state", {"repo": "hive"})[0]
    assert call(store, scope, "read_index", {"repo": "../x"})[0]


# ---------------------------------------------------------------------------
# propose_page
# ---------------------------------------------------------------------------
def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def gstore(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git binary not available")
    origin = str(tmp_path / "origin.git")
    git(str(tmp_path), "init", "--bare", "-q", origin)
    st = str(tmp_path / "store")
    git(str(tmp_path), "clone", "-q", origin, st)
    for k, v in (("user.email", "t@e.st"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(st, "config", k, v)
    os.makedirs(os.path.join(st, "pages"))
    page(__import__("pathlib").Path(st, "pages"), "gaal-a", ["repo:gaal"])
    git(st, "add", "-A")
    git(st, "commit", "-qm", "seed")
    git(st, "push", "-q", "-u", "origin", "HEAD")
    return {"store": st, "origin": origin}


GOOD = "---\nname: New\ndescription: d\ntype: feedback\n---\n\nbody\n"


def test_propose_page_creates_inbox_ref_and_leaves_store(gstore):
    st = gstore["store"]
    head = git(st, "rev-parse", "HEAD")
    err, text = call(st, remote(), "propose_page",
                     {"name": "new-page.md", "text": GOOD, "repo": "gaal"}, "me@example.com")
    assert not err
    ref = json.loads(text)["ref"]
    assert ref.startswith("refs/okfmem/inbox/")
    meta = json.loads(git(gstore["origin"], "show", f"{ref}:meta.json"))
    assert meta["source"] == "mcp" and meta["ref"] == "me@example.com"
    assert git(st, "rev-parse", "HEAD") == head
    assert git(st, "status", "--porcelain") == ""
    assert os.listdir(os.path.join(st, "pages")) == ["gaal-a.md"]


def test_propose_page_stdio_caller_is_local(gstore):
    err, text = call(gstore["store"], mm.LOCAL, "propose_page",
                     {"name": "new-page.md", "text": GOOD})
    assert not err
    ref = json.loads(text)["ref"]
    assert json.loads(git(gstore["origin"], "show", f"{ref}:meta.json"))["ref"] == "local"


def test_propose_page_rejections(gstore):
    st = gstore["store"]
    for args in (
        {"name": "MEMORY.md", "text": GOOD, "repo": "gaal"},
        {"name": "../x.md", "text": GOOD, "repo": "gaal"},
        {"name": "x.md", "text": "no frontmatter\n", "repo": "gaal"},
        {"name": "x.md", "text": GOOD, "repo": "hive"},
        {"name": "x.md", "text": GOOD},
    ):
        assert call(st, remote(), "propose_page", args)[0], args
    assert git(gstore["origin"], "for-each-ref", "refs/okfmem") == ""


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
def jwt(aud=AUD, iss=f"https://{TEAM}.cloudflareaccess.com", exp=NOW + 600):
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'aud': [aud], 'iss': iss, 'exp': exp})}.sig"


class Fetch:
    def __init__(self, result=(200, {"email": "me@example.com"})):
        self.result, self.calls = result, 0

    def __call__(self, team, token):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def access(fetch):
    return mm.Access(TEAM, AUD, ["Me@Example.com"], fetch=fetch, clock=lambda: NOW)


def test_access_accepts_and_caches_until_exp():
    f = Fetch()
    a, tok = access(f), jwt()
    assert a.check(tok) == (200, "me@example.com")
    assert a.check(tok) == (200, "me@example.com")
    assert f.calls == 1
    a.clock = lambda: NOW + 601
    assert a.check(tok)[0] == 401


@pytest.mark.parametrize("tok", [
    None, "", "garbage", jwt(aud="other"), jwt(iss="https://evil.cloudflareaccess.com"),
    jwt(exp=NOW - 1),
])
def test_access_claim_failures_are_401_even_when_identity_is_200(tok):
    f = Fetch()
    assert access(f).check(tok) == (401, None)
    assert f.calls == 0


def test_access_email_outside_allowlist_or_rejected_is_401():
    assert access(Fetch((200, {"email": "x@example.com"}))).check(jwt()) == (401, None)
    assert access(Fetch((403, None))).check(jwt()) == (401, None)
    assert access(Fetch((200, None))).check(jwt()) == (401, None)


def test_access_unreachable_is_503_and_not_cached():
    f = Fetch(mm.AccessUnavailable("down"))
    a = access(f)
    assert a.check(jwt()) == (503, None)
    assert a.check(jwt()) == (503, None)
    assert f.calls == 2
    assert access(Fetch((502, None))).check(jwt()) == (503, None)


@pytest.mark.parametrize("exc", [TimeoutError("slow"), IncompleteRead(b"")])
def test_fetch_identity_timeout_or_broken_reply_becomes_unavailable(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(mm._identity_opener, "open", boom)
    with pytest.raises(mm.AccessUnavailable):
        mm.fetch_identity(TEAM, "t")


def test_fetch_identity_does_not_follow_a_redirect():
    sent = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            sent.append(self.headers.get("Cookie"))
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/get-identity"
    req = urllib.request.Request(url, headers={"Cookie": "CF_Authorization=t"})
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            mm._identity_opener.open(req, timeout=10)
    finally:
        srv.shutdown()
        srv.server_close()
    assert e.value.code == 302
    assert sent == ["CF_Authorization=t"]
    assert access(Fetch((302, None))).check(jwt()) == (401, None)


def test_access_refuses_a_token_outside_the_jwt_alphabet():
    f = Fetch()
    assert access(f).check(jwt() + "; other=1") == (401, None)
    assert f.calls == 0


@pytest.fixture
def http(store):
    f = Fetch()
    srv = mm.make_http_server(store, remote(), access(f), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f, f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()
    srv.server_close()


def post(url, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Cf-Access-Jwt-Assertion"] = token
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


FIND = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "find", "arguments": {"terms": ["watch"]}}}


def test_http_without_or_with_bad_token_is_401_and_runs_no_tool(http, monkeypatch):
    f, url = http
    ran = []
    monkeypatch.setitem(mm.TOOLS, "find", (lambda *a: ran.append(1), "", {}, []))
    assert post(url, FIND)[0] == 401
    f.result = (401, None)
    assert post(url, FIND, jwt())[0] == 401
    assert ran == []


def test_http_valid_token_serves_scoped_find(http):
    _, url = http
    code, body = post(url, FIND, jwt())
    assert code == 200
    text = json.loads(body)["result"]["content"][0]["text"]
    assert [r["slug"] for r in json.loads(text)] == ["gaal-a"]


def test_http_unreachable_is_503(http):
    f, url = http
    f.result = mm.AccessUnavailable("down")
    assert post(url, FIND, jwt())[0] == 503


def test_http_notification_is_202_and_get_is_405(http):
    _, url = http
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert post(url, note, jwt())[0] == 202
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url, timeout=10)
    assert e.value.code == 405


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
BASE = ["mcp", "serve", "--http", "127.0.0.1:0", "--access-team", TEAM,
        "--access-aud", AUD, "--allow-email", "me@example.com", "--read-label", "*"]


def without(flag):
    out, i = [], 0
    while i < len(BASE):
        if BASE[i] == flag:
            i += 2
            continue
        out.append(BASE[i])
        i += 1
    return out


@pytest.mark.parametrize("flag", ["--read-label", "--access-team", "--access-aud", "--allow-email"])
def test_http_refuses_to_start_naming_missing_flag(store, capsys, flag):
    assert mm.main(without(flag) + ["--store", store]) == 2
    assert flag in capsys.readouterr().err.splitlines()[0]


@pytest.mark.parametrize("flag", ["--read-label", "--allow-email"])
def test_missing_flag_is_named_before_a_missing_store(tmp_path, capsys, flag):
    assert mm.main(without(flag) + ["--store", str(tmp_path / "none")]) == 2
    assert flag in capsys.readouterr().err.splitlines()[0]


def test_blank_or_invalid_read_label_refuses_to_start(store, capsys):
    argv = without("--read-label") + ["--store", store]
    assert mm.main(argv + ["--read-label", " "]) == 2
    assert "--read-label" in capsys.readouterr().err.splitlines()[0]
    assert mm.main(argv + ["--read-label", "repo:bad label"]) == 2
    assert "--read-label" in capsys.readouterr().err.splitlines()[0]


def test_http_refuses_non_loopback_without_bind_any(store, capsys):
    argv = [a.replace("127.0.0.1:0", "0.0.0.0:0") for a in BASE] + ["--store", store]
    assert mm.main(argv) == 2
    assert "--bind-any" in capsys.readouterr().err.splitlines()[0]


def test_needs_exactly_one_transport(store, capsys):
    assert mm.main(["mcp", "serve", "--store", store]) == 2


def test_imports_stdlib_only():
    import ast
    tree = ast.parse(open(mm.__file__, encoding="utf-8").read())
    local = {"memory_find", "memory_inbox", "memory_frontmatter"}
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        for n in names:
            top = n.split(".")[0]
            assert top in sys.stdlib_module_names or top in local, n


def test_dispatcher_registers_mcp_and_core_is_unchanged(store):
    run = [sys.executable, os.path.join(ROOT, "okfmem")]
    r = subprocess.run(run + ["mcp", "serve", "--stdio", "--store", store],
                       input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["id"] == 1
    r = subprocess.run(run + ["bogus"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "unknown command" in r.stderr
