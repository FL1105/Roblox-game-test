"""Local HTTP server for the terminal. Binds to loopback only and refuses foreign Host headers
(DNS-rebinding) and non-JSON POSTs (cross-site form posts)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .state import TerminalState

INDEX = Path(__file__).with_name("static") / "index.html"
MAX_BODY = 64 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def make_server(state: TerminalState, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SniperTerminal"

        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
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
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                window = parse_qs(url.query).get("range", ["24h"])[0]
                return self._json(200, state.snapshot("all" if window == "all" else "24h"))
            self._json(404, {"error": "not found"})

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
                return self._json(400, {"error": "body must be a JSON object under 64 KB"})

            path = urlparse(self.path).path
            try:
                if path == "/api/bot":
                    if not isinstance(data.get("running"), bool):
                        raise ValueError("running must be true or false")
                    state.set_running(data["running"])
                    return self._json(200, {"running": state.running})
                if path == "/api/settings":
                    return self._json(200, {"settings": state.update_settings(data)})
                if path == "/api/reset":
                    state.reset(data.get("start_sol"))
                    return self._json(200, {"ok": True, "start_sol": state.start_sol})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), Handler)
