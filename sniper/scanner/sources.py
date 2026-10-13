"""Thin read-only clients for Solana JSON-RPC, DexScreener and Jupiter quotes.

None of these touch a wallet or private key. Each takes an injectable `http`
callable so tests can run without a network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .models import RoundTrip

WSOL_MINT = "So11111111111111111111111111111111111111112"


class SourceError(Exception):
    """A data source failed or returned something unusable."""


def http_json(url: str, data: Any = None, timeout: float = 15) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": "sniper-scanner/0.1", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Jupiter answers "no route" with HTTP 400 and a JSON body we want to read.
        try:
            parsed = json.loads(e.read())
        except ValueError:
            raise SourceError(f"HTTP {e.code} from {url}") from e
        if isinstance(parsed, dict):
            parsed.setdefault("_http_status", e.code)
            return parsed
        raise SourceError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceError(f"{url}: {e}") from e
    try:
        return json.loads(raw)
    except ValueError as e:
        raise SourceError(f"invalid JSON from {url}") from e


Http = Callable[..., Any]


class SolanaRpc:
    def __init__(self, url: str, http: Http = http_json):
        self.url = url
        self._http = http

    def call(self, method: str, params: list) -> Any:
        resp = self._http(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(resp, dict) or "result" not in resp:
            err = resp.get("error") if isinstance(resp, dict) else resp
            raise SourceError(f"{method} failed: {err}")
        return resp["result"]

    def get_account(self, address: str) -> dict | None:
        return self.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])["value"]

    def get_multiple_accounts(self, addresses: list[str]) -> list[dict | None]:
        out: list[dict | None] = []
        for i in range(0, len(addresses), 100):  # RPC limit per call
            out += self.call("getMultipleAccounts", [addresses[i : i + 100], {"encoding": "jsonParsed"}])["value"]
        return out

    def get_token_supply(self, mint: str) -> int:
        return int(self.call("getTokenSupply", [mint])["value"]["amount"])

    def get_largest_accounts(self, mint: str) -> list[dict]:
        return self.call("getTokenLargestAccounts", [mint])["value"]


class DexScreener:
    def __init__(self, http: Http = http_json, base: str = "https://api.dexscreener.com/latest/dex"):
        self._http = http
        self._base = base

    def pairs(self, mint: str) -> list[dict]:
        resp = self._http(f"{self._base}/tokens/{mint}")
        if not isinstance(resp, dict):
            raise SourceError("unexpected DexScreener response")
        return resp.get("pairs") or []


class JupiterQuotes:
    """Quote-based exit check. A quote is not a simulation: it shows a route and price exist,
    not that a real sell transaction would succeed."""

    def __init__(self, http: Http = http_json, base: str = "https://lite-api.jup.ag/swap/v1"):
        self._http = http
        self._base = base

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100) -> tuple[int | None, str | None]:
        qs = urllib.parse.urlencode(
            {"inputMint": input_mint, "outputMint": output_mint, "amount": amount, "slippageBps": slippage_bps}
        )
        resp = self._http(f"{self._base}/quote?{qs}")
        if isinstance(resp, dict) and "outAmount" in resp:
            return int(resp["outAmount"]), None
        err = resp.get("errorCode") or resp.get("error") if isinstance(resp, dict) else None
        return None, str(err or "no route")

    def round_trip(self, mint: str, in_lamports: int) -> RoundTrip:
        tokens, buy_err = self.quote(WSOL_MINT, mint, in_lamports)
        if tokens is None:
            return RoundTrip(in_lamports, None, None, buy_error=buy_err)
        back, sell_err = self.quote(mint, WSOL_MINT, tokens)
        return RoundTrip(in_lamports, tokens, back, sell_error=sell_err)
