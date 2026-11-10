#!/usr/bin/env python3
"""okfmem mcp — an MCP server over the store: stdio locally, Access-gated HTTP remotely (#147).

    okfmem mcp serve --stdio [--store PATH]
    okfmem mcp serve --http 127.0.0.1:PORT --access-team TEAM --access-aud AUD
                     --allow-email EMAIL... --read-label LABEL... [--read-type-user]
                     [--bind-any] [--store PATH]

Tools: find, read_page, read_index, read_state, propose_page. The only write is
propose_page, which pushes to the inbox (`okfmem inbox review` accepts it); nothing
here edits pages/, state/ or views/. stdio is trusted like the local file system.
HTTP serves only a Cloudflare tunnel: every request needs a valid Access token, and
the read scope is an allowlist of labels. Standard library only.
"""

import argparse
import base64
import http.client
import ipaddress
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _ROOT_DIR)
import memory_find  # noqa: E402
import memory_inbox  # noqa: E402
from memory_frontmatter import (  # noqa: E402
    find_frontmatter,
    normalize_label,
    parse_fields,
    parse_labels,
)

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
IDENTITY_TIMEOUT = 10
REQUEST_TIMEOUT = 30
MAX_BODY = 1_000_000
MAX_PAGE_TEXT = 200_000
CACHE_MAX = 1024
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TEAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
AUD_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# The token goes into a Cookie header, so only the JWT alphabet passes.
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


class ToolError(Exception):
    pass


class Scope:
    """What a caller may read. `labels` None means everything (stdio)."""

    def __init__(self, labels=None, allow_user=True):
        self.labels = None if labels is None else {x.lower() for x in labels}
        self.allow_user = allow_user

    def label_ok(self, label):
        return self.labels is None or "*" in self.labels or label.lower() in self.labels

    @property
    def unrestricted(self):
        return self.labels is None and self.allow_user

    def page_ok(self, labels, ptype):
        if ptype.lower() == "user" and not self.allow_user:
            return False
        if self.labels is None:
            return True
        # `*` allows every label, so a page with no label stays hidden (#147).
        return any("*" in self.labels or x.lower() in self.labels for x in labels)

    def repo_ok(self, repo):
        return self.label_ok(f"repo:{repo}")


LOCAL = Scope()
_SKIP_UPPER = {n.upper() for n in memory_find.SKIP}


def _load_page(store, slug):
    """(text, labels, type) of pages/<slug>.md, or None when absent or unreadable."""
    # Case-blind: on macOS and Windows `memory` opens MEMORY.md.
    if not SLUG_RE.match(slug or "") or f"{slug}.md".upper() in _SKIP_UPPER:
        return None
    path = os.path.join(store, "pages", f"{slug}.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    fm = find_frontmatter(text)
    labels = parse_labels(text, fm, warn=None) if fm else []
    ptype = (parse_fields(text, fm).get("type") or "") if fm else ""
    return text, labels, ptype


def _need_str(args, key, required=True):
    v = args.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"{key} must be a non-empty string")
    return v


def tool_find(store, scope, args):
    terms = args.get("terms")
    if isinstance(terms, str):
        terms = terms.split()
    if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
        raise ToolError("terms must be a list of strings")
    terms = [t for t in terms if t.strip()]
    if not terms:
        raise ToolError("give at least one search term")
    repo = _need_str(args, "repo", required=False)
    limit = args.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ToolError("limit must be 0 (no cap) or more")
    if scope.unrestricted:
        return memory_find.find(store, terms, repo, False, limit)
    hits = memory_find.find(store, terms, repo, False, 0)
    out = []
    for r in hits:
        page = _load_page(store, r["slug"])
        if page and scope.page_ok(page[1], page[2]):
            out.append(r)
    return out[:limit] if limit else out


def tool_read_page(store, scope, args):
    slug = _need_str(args, "slug")
    page = _load_page(store, slug)
    if page is None or not scope.page_ok(page[1], page[2]):
        raise ToolError(f"no such page: {slug}")
    return {"slug": slug, "text": page[0]}


def _repo_file(store, scope, args, parts, what):
    repo = _need_str(args, "repo")
    if not SLUG_RE.match(repo) or not scope.repo_ok(repo):
        raise ToolError(f"no {what} for repo: {repo}")
    path = os.path.join(store, *(p.format(repo=repo) for p in parts))
    try:
        with open(path, encoding="utf-8") as f:
            return {"repo": repo, "text": f.read()}
    except (OSError, UnicodeDecodeError):
        raise ToolError(f"no {what} for repo: {repo}") from None


def tool_read_index(store, scope, args):
    return _repo_file(store, scope, args, ("views", "{repo}", "MEMORY.md"), "index")


def tool_read_state(store, scope, args):
    return _repo_file(store, scope, args, ("state", "{repo}.md"), "state")


def tool_propose_page(store, scope, args, caller="local"):
    name = _need_str(args, "name")
    text = _need_str(args, "text")
    repo = _need_str(args, "repo", required=scope.labels is not None)
    if not memory_inbox.importable_name(name):
        raise ToolError(f"invalid page name: {name}")
    if len(text) > MAX_PAGE_TEXT:
        raise ToolError("page text is too large")
    if memory_inbox.page_fields(text) is None:
        raise ToolError(
            "page needs OKF frontmatter with "
            f"{', '.join(memory_inbox.REQUIRED_FIELDS)}"
        )
    if repo is not None and not scope.repo_ok(repo):
        raise ToolError(f"repo is not allowed: {repo}")
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        code, ref = memory_inbox.push(
            store, tmp, source="mcp", repo=repo, ref=caller, out=lines.append
        )
    if code != 0 or not ref:
        raise ToolError("; ".join(lines) or "inbox push failed")
    return {"ref": ref}


_STR = {"type": "string"}
TOOLS = {
    "find": (
        tool_find,
        "Rank durable pages by search terms (same ranking as `okfmem find`).",
        {
            "terms": {"type": "array", "items": _STR},
            "repo": _STR,
            "limit": {"type": "integer"},
        },
        ["terms"],
    ),
    "read_page": (tool_read_page, "Read one page by slug.", {"slug": _STR}, ["slug"]),
    "read_index": (
        tool_read_index,
        "Read views/<repo>/MEMORY.md.",
        {"repo": _STR},
        ["repo"],
    ),
    "read_state": (
        tool_read_state,
        "Read state/<repo>.md.",
        {"repo": _STR},
        ["repo"],
    ),
    "propose_page": (
        tool_propose_page,
        "Propose a new page (`name` ends in .md; `text` has OKF frontmatter with "
        "name, description, type) to the inbox for review; never edits the store. "
        "`repo` is the project name of its `repo:` label.",
        {"name": _STR, "text": _STR, "repo": _STR},
        ["name", "text"],
    ),
}


def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_message(store, scope, msg, caller="local"):
    """One JSON-RPC message in, response dict out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "invalid request")
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if "id" not in msg:
        return None
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {
            "protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "okfmem", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": n,
                    "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": req},
                }
                for n, (_fn, desc, props, req) in TOOLS.items()
            ]
        }
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in TOOLS:
            return _rpc_error(rid, -32602, f"unknown tool: {name}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _rpc_error(rid, -32602, "arguments must be an object")
        try:
            fn = TOOLS[name][0]
            data = fn(store, scope, args, caller) if name == "propose_page" else fn(store, scope, args)
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": False}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception:
            # One bad call must not end a stdio session or leak a traceback remotely.
            return _rpc_error(rid, -32603, "internal error")
    else:
        return _rpc_error(rid, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def handle_text(store, scope, text, caller="local"):
    """Parse and dispatch; returns the response dict or None."""
    try:
        msg = json.loads(text)
    except (ValueError, RecursionError):
        return _rpc_error(None, -32700, "parse error")
    return handle_message(store, scope, msg, caller)


def serve_stdio(store, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        resp = handle_text(store, LOCAL, line)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


# ---------------------------------------------------------------------------
# Cloudflare Access
# ---------------------------------------------------------------------------
class AccessUnavailable(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib forwards the Cookie header on a redirect; a 3xx becomes an HTTPError.
    def redirect_request(self, *args, **kwargs):
        return None


_identity_opener = urllib.request.build_opener(_NoRedirect)


def fetch_identity(team, token):
    """(status, json or None) from the team's get-identity endpoint.
    Network failure raises AccessUnavailable."""
    req = urllib.request.Request(
        f"https://{team}.cloudflareaccess.com/cdn-cgi/access/get-identity",
        headers={"Cookie": f"CF_Authorization={token}"},
    )
    try:
        with _identity_opener.open(req, timeout=IDENTITY_TIMEOUT) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise AccessUnavailable(str(e)) from e
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        return status, None


def token_claims(token):
    if not JWT_RE.match(token):
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError, UnicodeError):
        return None
    return claims if isinstance(claims, dict) else None


class Access:
    """Verdicts: (200, email), (401, None) or (503, None). Never a pass on doubt."""

    def __init__(self, team, aud, emails, fetch=fetch_identity, clock=time.time):
        self.team, self.aud = team, aud
        self.emails = {e.lower() for e in emails}
        self.fetch, self.clock = fetch, clock
        self._cache = {}
        self._lock = threading.Lock()

    def check(self, token):
        claims = token_claims(token) if token else None
        if not claims:
            return 401, None
        exp, now = claims.get("exp"), self.clock()
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if (
            isinstance(exp, bool)
            or not isinstance(exp, (int, float))
            or exp <= now
            or self.aud not in auds
            or claims.get("iss") != f"https://{self.team}.cloudflareaccess.com"
        ):
            return 401, None
        with self._lock:
            hit = self._cache.get(token)
            if hit and hit[1] > now:
                return 200, hit[0]
        try:
            status, ident = self.fetch(self.team, token)
        except AccessUnavailable:
            return 503, None
        if status >= 500:
            return 503, None
        email = ident.get("email") if status == 200 and isinstance(ident, dict) else None
        if not isinstance(email, str) or email.lower() not in self.emails:
            return 401, None
        with self._lock:
            for k in [k for k, v in self._cache.items() if v[1] <= now]:
                del self._cache[k]
            if len(self._cache) < CACHE_MAX:
                self._cache[token] = (email, exp)
        return 200, email


def make_http_server(store, scope, access, host, port):
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/mcp":
                return self._send(404)
            code, email = access.check(self.headers.get("Cf-Access-Jwt-Assertion"))
            if code != 200:
                msg = "unauthorized" if code == 401 else "access check unavailable"
                return self._send(code, json.dumps({"error": msg}).encode())
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._send(400, b'{"error": "bad request body"}')
            resp = handle_text(store, scope, self.rfile.read(length).decode("utf-8", "replace"), email)
            if resp is None:
                return self._send(202)
            self._send(200, json.dumps(resp).encode())

        def do_GET(self):
            self._send(405, b'{"error": "POST /mcp only"}')

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _die(message):
    print(f"okfmem mcp serve: {message}", file=sys.stderr)
    return 2


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="okfmem mcp", description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["mcp"])
    ap.add_argument("action", choices=["serve"])
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--http", metavar="HOST:PORT")
    ap.add_argument("--access-team")
    ap.add_argument("--access-aud")
    ap.add_argument("--allow-email", nargs="+", action="extend", default=[])
    ap.add_argument("--read-label", nargs="+", action="extend", default=[])
    ap.add_argument("--read-type-user", action="store_true")
    ap.add_argument("--bind-any", action="store_true")
    ap.add_argument("--store", default=DEFAULT_STORE)
    args = ap.parse_args(argv)

    if args.stdio == bool(args.http):
        return _die("give exactly one of --stdio or --http HOST:PORT")
    if args.stdio:
        if not os.path.isdir(os.path.join(args.store, "pages")):
            return _die(f"no pages/ under {args.store}; pass --store")
        # MCP stdio is UTF-8 with "\n" framing, whatever the platform's console default.
        for stream, kw in ((sys.stdin, {}), (sys.stdout, {"newline": "\n"})):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", **kw)
        serve_stdio(args.store)
        return 0

    emails = [e.strip() for e in args.allow_email if e.strip()]
    labels = [x.strip() for x in args.read_label if x.strip()]
    for flag, val in (
        ("--access-team", args.access_team),
        ("--access-aud", args.access_aud),
        ("--allow-email", emails),
        ("--read-label", labels),
    ):
        if not val:
            hint = " (use --read-label '*' to allow every label)" if flag == "--read-label" else ""
            return _die(f"{flag} is required with --http{hint}")
    bad = [x for x in labels if x != "*" and normalize_label(x) is None]
    if bad:
        return _die(f"--read-label {bad[0]!r} is not a valid label")
    labels = [x if x == "*" else normalize_label(x) for x in labels]
    if not TEAM_RE.match(args.access_team) or not AUD_RE.match(args.access_aud):
        return _die("--access-team and --access-aud must be plain identifiers")
    host, _, port = args.http.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or int(port) > 65535:
        return _die("--http needs HOST:PORT, e.g. 127.0.0.1:8787")
    if not _is_loopback(host) and not args.bind_any:
        return _die(f"--bind-any is required to listen on non-loopback host {host}")
    if not os.path.isdir(os.path.join(args.store, "pages")):
        return _die(f"no pages/ under {args.store}; pass --store")

    scope = Scope(labels, allow_user=args.read_type_user)
    access = Access(args.access_team, args.access_aud, emails)
    try:
        server = make_http_server(args.store, scope, access, host, int(port))
    except OSError as e:
        return _die(f"cannot listen on {args.http}: {e}")
    print(f"okfmem mcp serve: listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
