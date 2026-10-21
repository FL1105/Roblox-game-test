"""Minimal rippled JSON-RPC client (standard library only). Read-only: it can ask, never sign or submit."""

from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal
from typing import Callable

from .model import Asset

DEFAULT_URLS = ("https://s1.ripple.com:51234/", "https://s2.ripple.com:51234/", "https://xrplcluster.com/")
TRANSIENT = {"slowDown", "tooBusy", "noNetwork", "noCurrent", "noClosed"}
RATE_UNITY = Decimal(1_000_000_000)


class XrplError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _http_post(url: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class XrplClient:
    def __init__(self, urls=DEFAULT_URLS, timeout: float = 15.0, retries: int = 3,
                 post: Callable[[str, bytes, float], bytes] | None = None):
        self.urls = tuple(urls)
        self.timeout = timeout
        self.retries = retries
        self.post = post or _http_post
        self._preferred = 0  # index of the last server that answered; tried first next time

    def call(self, method: str, **params) -> dict:
        """Result object of one request. Network errors and transient server errors are retried on the
        next server; a ledger error (e.g. actNotFound) raises XrplError straight away."""
        body = json.dumps({"method": method, "params": [params]}).encode()
        last: Exception | str = ""
        start = self._preferred
        for attempt in range(self.retries + 1):
            idx = (start + attempt) % len(self.urls)
            try:
                result = json.loads(self.post(self.urls[idx], body, self.timeout))["result"]
            except (OSError, ValueError, KeyError) as e:  # URLError and HTTPError are OSErrors
                last = e
            else:
                code = str(result.get("error", "unknown")) if result.get("status") == "error" else None
                if code not in TRANSIENT:  # answered properly, even if the answer is a ledger error
                    self._preferred = idx
                    if code is None:
                        return result
                    raise XrplError(code, str(result.get("error_message", "")))
                last = code
            time.sleep(min(0.5 * 2 ** attempt, 4.0))
        raise XrplError("unreachable", f"{method} failed after {self.retries + 1} attempts: {last}")

    def validated_ledger(self) -> int:
        """Index of the newest validated ledger, to pin a set of reads to one state of the world."""
        return int(self.call("ledger", ledger_index="validated")["ledger_index"])

    def book_offers(self, taker_gets: Asset, taker_pays: Asset, limit: int = 60, ledger="validated") -> dict:
        return self.call("book_offers", taker_gets=taker_gets.spec(), taker_pays=taker_pays.spec(),
                         limit=limit, ledger_index=ledger)

    def amm_info(self, a: Asset, b: Asset, ledger="validated") -> dict:
        return self.call("amm_info", asset=a.spec(), asset2=b.spec(), ledger_index=ledger)

    def transfer_rate(self, issuer: str) -> Decimal:
        """The issuer's TransferRate as a multiplier (1.005 = 0.5% fee); 1 when it charges none."""
        data = self.call("account_info", account=issuer, ledger_index="validated")["account_data"]
        return Decimal(data.get("TransferRate", RATE_UNITY)) / RATE_UNITY
