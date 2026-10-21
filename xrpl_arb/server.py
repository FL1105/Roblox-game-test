"""Local HTTP server for the dashboard. Binds to loopback only and refuses foreign Host headers (DNS rebinding) and non-JSON POSTs
(cross-site form posts). `/api/stream` is a Server-Sent Events feed: a full snapshot every time anything changes."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .state import BotState

INDEX = Path(__file__).with_name("static") / "index.html"
MAX_BODY = 16 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"


def make_server(state: BotState, host: str = "127.0.0.1", port: int = 8780) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "XrplArb"

        def log_message(self, *args):  # keep the console quiet
            pass

        def _headers(self, code: int, ctype: str, length: int | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self._headers(code, ctype, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            raw = self.headers.get("Host") or ""
            name = raw if raw.endswith("]") else raw.rsplit(":", 1)[0]
            return name in LOCAL_HOSTS

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(200, state.snapshot())
            if path == "/api/stream":
                return self._stream()
            self._json(404, {"error": "not found"})

        def _stream(self) -> None:
            self._headers(200, "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            seen = -1
            try:
                while True:
                    v = state.wait(seen, 15.0)
                    if v != seen:
                        seen = v
                        self.wfile.write(b"data: " + json.dumps(state.snapshot()).encode() + b"\n\n")
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "content-type must be application/json"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= MAX_BODY:
                    raise ValueError
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "body must be a JSON object under 16 KB"})
            path = urlparse(self.path).path
            try:
                if path == "/api/bot":
                    if not isinstance(data.get("running"), bool):
                        raise ValueError("running must be true or false")
                    state.set_running(data["running"])
                    return self._json(200, {"running": state.running})
                if path == "/api/settings":
                    new = state.cfg.update(data)
                    state.change(lambda: setattr(state, "cfg", new))
                    state.log("info", "settings changed: " + ", ".join(f"{k}={v}" for k, v in data.items()))
                    return self._json(200, {"settings": new.tunables()})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), Handler)
