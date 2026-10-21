"""Minimal rippled JSON-RPC client (standard library only) with failover between public servers."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

COOL_OFF_S = 20.0  # a server that just failed is tried last for this long; each further failure in a row doubles it
MAX_COOL_OFF_S = 600.0
RETRY_PAUSE_S = 0.25
TRANSIENT = ("slowDown", "tooBusy", "noNetwork", "noCurrent", "noClosed")


class RpcError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class Rpc:
    def __init__(self, urls: tuple[str, ...] | list[str], timeout: float = 15.0):
        if not urls:
            raise ValueError("need at least one server URL")
        self.urls, self.timeout = list(urls), timeout
        self._bad_until = {u: 0.0 for u in self.urls}
        self._fails = {u: 0 for u in self.urls}  # failures in a row, per server
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        """The server calls are going to right now."""
        return self._order()[0]

    def _order(self) -> list[str]:
        now = time.time()
        with self._lock:
            return sorted(self.urls, key=lambda u: (self._bad_until[u] > now, self.urls.index(u)))

    def _fail(self, url: str) -> None:
        with self._lock:
            self._fails[url] += 1
            self._bad_until[url] = time.time() + min(COOL_OFF_S * 2 ** (self._fails[url] - 1), MAX_COOL_OFF_S)

    def _ok(self, url: str) -> None:
        with self._lock:
            self._fails[url] = 0

    def _post(self, url: str, body: bytes) -> dict:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "xrpl-arb"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)["result"]

    def call(self, method: str, **params) -> dict:
        """Result dict on success. RpcError for a ledger-level error (e.g. actNotFound); OSError once every server has failed."""
        body = json.dumps({"method": method, "params": [params]}).encode()
        last: Exception | None = None
        for url in self._order():
            for attempt in range(2):  # one quick retry: connection resets on public servers are usually momentary
                try:
                    result = self._post(url, body)
                except (urllib.error.URLError, OSError, ValueError, KeyError) as e:  # network, HTTP error, bad JSON
                    last = e
                    if attempt == 0:
                        time.sleep(RETRY_PAUSE_S)
                        continue
                    self._fail(url)
                    break
                if result.get("status") == "error":
                    if result.get("error") in TRANSIENT:
                        last = RpcError(result["error"], result.get("error_message", ""))
                        if attempt == 0:
                            time.sleep(RETRY_PAUSE_S * 4)
                            continue
                        self._fail(url)
                        break
                    raise RpcError(result.get("error", "unknown"), result.get("error_message", ""))
                self._ok(url)
                return result
        raise OSError(f"all XRPL servers failed: {last}")
