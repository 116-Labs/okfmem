"""Reusable local page shell: a short-lived stdlib HTTP server for okfmem pages.

Binds 127.0.0.1 only, on a random port; every request (POSTs included) must carry
the one-time token, else 403. Pages are static files from `webui/`; JSON endpoints
are plain callables. Stops on `stop()`, Ctrl-C or an idle timeout. Used by
`okfmem inbox review --web`; a later `okfmem ui` adds pages, not a second server.
"""
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "webui")
LOOPBACK = "127.0.0.1"
MAX_BODY = 8 * 1024 * 1024
TOKEN_HEADER = "X-Okfmem-Token"


class PageServer:
    """`pages`: {url path: file name under webui/}.
    `api`: {url path: fn(payload dict | None) -> (status, JSON-able)}; GET passes
    None, POST passes the decoded JSON body."""

    def __init__(self, pages, api, idle_timeout=900, web_dir=WEB_DIR):
        self.token = secrets.token_urlsafe(24)
        self.pages, self.api, self.web_dir = pages, api, web_dir
        self.idle_timeout = idle_timeout
        self._last = time.monotonic()
        self._stop = threading.Event()
        self.httpd = ThreadingHTTPServer((LOOPBACK, 0), self._handler())
        self.httpd.timeout = 0.5

    @property
    def port(self):
        return self.httpd.server_address[1]

    def url(self, path="/"):
        return f"http://{LOOPBACK}:{self.port}{path}?t={self.token}"

    def stop(self):
        self._stop.set()

    def serve(self, open_browser=True):
        """Block until stopped, interrupted or idle; returns why."""
        if open_browser:
            try:
                webbrowser.open(self.url())
            except webbrowser.Error:
                pass
        reason = "done"
        try:
            while not self._stop.is_set():
                self.httpd.handle_request()
                if time.monotonic() - self._last > self.idle_timeout:
                    reason = "idle timeout"
                    break
        except KeyboardInterrupt:
            reason = "interrupted"
        finally:
            self.httpd.server_close()
        return reason

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _length(self):
                try:
                    return int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return -1

            def _authorised(self, query):
                given = self.headers.get(TOKEN_HEADER) or (query.get("t") or [""])[0]
                return hmac.compare_digest(given.encode(), server.token.encode())

            def _refuse(self, code):
                length = self._length()
                if 0 < length <= MAX_BODY:
                    self.rfile.read(length)  # unread bytes make Windows reset the socket
                self.close_connection = True
                self._send(code, json.dumps({"error": "forbidden" if code == 403
                                             else "not found"}).encode())

            def _route(self, method):
                url = urlsplit(self.path)
                if not self._authorised(parse_qs(url.query)):
                    return self._refuse(403)
                server._last = time.monotonic()
                if method == "GET" and url.path in server.pages:
                    path = os.path.join(server.web_dir, server.pages[url.path])
                    with open(path, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                fn = server.api.get(url.path)
                if fn is None:
                    return self._refuse(404)
                payload = None
                if method == "POST":
                    length = self._length()
                    if not 0 < length <= MAX_BODY:
                        return self._send(400, b'{"error": "bad request body"}')
                    try:
                        payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    except ValueError:
                        return self._send(400, b'{"error": "body is not JSON"}')
                code, result = fn(payload)
                self._send(code, json.dumps(result).encode())

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def log_message(self, *args):
                pass

        return Handler
