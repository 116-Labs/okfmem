"""Caller-supplied token and commit author for `okfmem pull` / `okfmem sync` (#140).

The remote is a real smart-HTTP git server (`git http-backend` behind Basic auth
on 127.0.0.1), so the token travels the same credential path it takes to GitHub.
"""
import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import memory_pull as mp
import memory_sync as ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")

TOKEN = "ghs_okfmemTestToken140"
WRONG = "ghs_wrongTokenNotAccepted"
AUTHOR = "bot <bot@users.noreply.github.com>"


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


class _GitHTTP(http.server.BaseHTTPRequestHandler):
    root = ""

    def log_message(self, *_a):
        pass

    def do_GET(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def _serve(self):
        good = "Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != good:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="okfmem-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {**os.environ, "GIT_PROJECT_ROOT": self.root, "GIT_HTTP_EXPORT_ALL": "1",
               "PATH_INFO": path, "QUERY_STRING": query,
               "REQUEST_METHOD": self.command, "REMOTE_USER": "x-access-token",
               "REMOTE_ADDR": "127.0.0.1", "CONTENT_LENGTH": str(len(body)),
               "CONTENT_TYPE": self.headers.get("Content-Type", ""),
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
        out = subprocess.run(["git", "http-backend"], input=body, env=env,
                             capture_output=True).stdout
        sep = b"\r\n\r\n" if b"\r\n\r\n" in out else b"\n\n"
        head, _, payload = out.partition(sep)
        status, headers = 200, []
        for line in head.decode("latin-1").splitlines():
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            elif key:
                headers.append((key, value.strip()))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Isolated git config plus an auth-gated HTTP remote; yields its base URL."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for var in ("GIT_ASKPASS", "SSH_ASKPASS", "OKFMEM_GIT_TOKEN", "OKFMEM_GIT_AUTHOR"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "srv"
    root.mkdir()
    handler = type("Handler", (_GitHTTP,), {"root": str(root)})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _identity(repo):
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def _write(repo, rel, text):
    Path(repo, rel).write_text(text, encoding="utf-8")


def _http_store(tmp_path, root, base, name="team"):
    """A bare origin served at `<base>/<name>.git` and a clone tracking it over HTTP."""
    origin = str(root / f"{name}.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    store = str(tmp_path / name)
    _git(str(tmp_path), "clone", "-q", origin, store)
    _identity(store)
    _write(store, "page.md", "v1\n")
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "seed")
    _git(store, "push", "-q", "-u", "origin", "HEAD")
    _git(store, "remote", "set-url", "origin", f"{base}/{name}.git")
    return origin, store


def _advance(tmp_path, origin, text):
    w = str(tmp_path / "writer")
    _git(str(tmp_path), "clone", "-q", origin, w)
    _identity(w)
    _write(w, "page.md", text)
    _git(w, "commit", "-qam", "advance")
    _git(w, "push", "-q", "origin", "HEAD")


def _run(main, argv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", None)
    with pytest.raises(SystemExit) as e:
        main()
    out = capsys.readouterr()
    return e.value.code, out.out + out.err


def test_store_token_picks_generic_or_per_store_variable():
    env = {"OKFMEM_GIT_TOKEN": "g", "OKFMEM_GIT_TOKEN_TEAM_A_X": "t"}
    assert ms.store_token(environ=env) == "g"
    assert ms.store_token("team-a.x", environ=env) == "t"
    assert ms.store_token("other", environ=env) is None
    assert ms.store_token(environ={"OKFMEM_GIT_TOKEN": ""}) is None


def test_parse_author_accepts_name_and_email_only():
    assert ms.parse_author(AUTHOR) == ("bot", "bot@users.noreply.github.com")
    for bad in ("bot", "<a@b>", "bot <a b@c>", "bot a@b", ""):
        assert ms.parse_author(bad) is None


def test_pull_with_token_fetches_over_http_and_stores_no_credential(
        tmp_path, server, monkeypatch, capsys):
    root, base = server
    origin, store = _http_store(tmp_path, root, base)
    _advance(tmp_path, origin, "v2\n")
    creds = [tmp_path / "plain-creds", tmp_path / "url-creds"]
    _git(store, "config", "credential.helper", f"store --file {creds[0].as_posix()}")
    _git(store, "config", f"credential.{base}.helper",
         f"store --file {creds[1].as_posix()}")

    assert mp.pull_store(store)["offline"] is True  # the remote does require auth

    monkeypatch.setenv("OKFMEM_GIT_TOKEN", TOKEN)
    code, out = _run(mp.main, ["okfmem", "--store", store], monkeypatch, capsys)

    assert code == 0
    assert "okfmem pull: fast-forwarded." in out
    assert Path(store, "page.md").read_text(encoding="utf-8") == "v2\n"
    assert TOKEN not in Path(store, ".git", "config").read_text(encoding="utf-8")
    for f in creds:
        assert not f.exists() or TOKEN not in f.read_text(encoding="utf-8")
    assert TOKEN not in out


def test_pull_with_bad_token_fails_on_one_line_naming_the_store(
        tmp_path, server, monkeypatch, capsys):
    root, base = server
    _origin, store = _http_store(tmp_path, root, base)
    monkeypatch.setenv("OKFMEM_GIT_TOKEN", WRONG)

    code, out = _run(mp.main, ["okfmem", "--quiet", "--store", store],
                     monkeypatch, capsys)

    assert code == 1
    line = next(ln for ln in out.splitlines() if ln.startswith("okfmem pull: "))
    assert line.startswith(f"okfmem pull: {os.path.abspath(store)}: fetch failed: ")
    assert WRONG not in out
    res = mp.pull_store(store, token=WRONG)
    assert res["auth_failed"] is True and res["offline"] is False
    assert "\n" not in res["reason"]


def test_attached_store_takes_only_its_own_token(tmp_path, server, monkeypatch):
    root, base = server
    origin, team = _http_store(tmp_path, root, base, name="team-a")
    _advance(tmp_path, origin, "v2\n")
    home = tmp_path / "home"
    home.mkdir()
    _write(home, "registry.json", json.dumps(
        {"map": {}, "overrides": {}, "stores": [{"name": "team-a", "path": team}]}))

    monkeypatch.setenv("OKFMEM_GIT_TOKEN", TOKEN)
    [(_a, res)] = mp.pull_attached(str(home))
    assert res["pulled"] is False  # the home store's token never goes to team-a

    monkeypatch.setenv("OKFMEM_GIT_TOKEN_TEAM_A", TOKEN)
    [(_a, res)] = mp.pull_attached(str(home))
    assert res["pulled"] is True
    assert Path(team, "page.md").read_text(encoding="utf-8") == "v2\n"


def test_attached_stores_sharing_a_token_variable_get_no_token(
        tmp_path, server, monkeypatch):
    root, base = server
    origin_a, team_a = _http_store(tmp_path, root, base, name="team-a")
    _advance(tmp_path, origin_a, "v2\n")
    _origin_b, team_b = _http_store(tmp_path, root, base, name="team.a")
    home = tmp_path / "home"
    home.mkdir()
    _write(home, "registry.json", json.dumps(
        {"map": {}, "overrides": {}, "stores": [{"name": "team-a", "path": team_a},
                                               {"name": "team.a", "path": team_b}]}))
    monkeypatch.setenv("OKFMEM_GIT_TOKEN_TEAM_A", TOKEN)

    results = mp.pull_attached(str(home))

    assert [a["name"] for a, _ in results] == ["team-a", "team.a"]
    for _a, res in results:
        assert res["pulled"] is False
        assert "OKFMEM_GIT_TOKEN_TEAM_A not used" in res["reason"]
    assert Path(team_a, "page.md").read_text(encoding="utf-8") == "v1\n"


def test_sync_with_token_and_author_pushes_a_commit_by_that_author(
        tmp_path, server, monkeypatch, capsys):
    root, base = server
    origin, store = _http_store(tmp_path, root, base)
    _write(store, "new.md", "hello\n")
    monkeypatch.setenv("OKFMEM_GIT_TOKEN", TOKEN)

    code, out = _run(ms.main, ["okfmem-sync", "--store", store, "-m", "bot save",
                               "--author", AUTHOR], monkeypatch, capsys)

    assert code == 0, out
    assert "pushed." in out
    got = _git(origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s")
    assert got == f"{AUTHOR}|{AUTHOR}|bot save"
    assert TOKEN not in Path(store, ".git", "config").read_text(encoding="utf-8")
    assert TOKEN not in out


def test_sync_with_bad_token_commits_nothing_and_fails_on_one_line(
        tmp_path, server, monkeypatch, capsys):
    root, base = server
    _origin, store = _http_store(tmp_path, root, base)
    _write(store, "new.md", "hello\n")
    before = _git(store, "rev-parse", "HEAD")
    monkeypatch.setenv("OKFMEM_GIT_TOKEN", WRONG)

    code, out = _run(ms.main, ["okfmem-sync", "--store", store, "-m", "x"],
                     monkeypatch, capsys)

    assert code == 1
    assert out.splitlines()[0].startswith(
        f"okfmem sync: {os.path.abspath(store)}: fetch failed: ")
    assert len(out.splitlines()) == 1
    assert WRONG not in out
    assert _git(store, "rev-parse", "HEAD") == before


def test_sync_author_from_env_applies_to_that_commit_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    store = str(tmp_path / "store")
    _git(str(tmp_path), "init", "-q", store)
    _identity(store)
    _write(store, "a.md", "a\n")
    monkeypatch.setenv("OKFMEM_GIT_AUTHOR", AUTHOR)

    code, _out = _run(ms.main, ["okfmem-sync", "--store", store, "-m", "m",
                                "--no-push"], monkeypatch, capsys)

    assert code == 0
    assert _git(store, "log", "-1", "--format=%an <%ae>") == AUTHOR
    assert _git(store, "config", "user.name") == "tester"


def test_sync_rejects_a_malformed_author(tmp_path, monkeypatch, capsys):
    code, out = _run(ms.main, ["okfmem-sync", "--store", str(tmp_path), "-m", "m",
                               "--author", "just-a-name"], monkeypatch, capsys)
    assert code == 2
    assert "name <email>" in out
