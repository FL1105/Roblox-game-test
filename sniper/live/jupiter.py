"""Rate-limited async Jupiter quote client. Read-only: it only asks for prices."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request

WSOL = "So11111111111111111111111111111111111111112"
BASE = "https://lite-api.jup.ag/swap/v1/quote"


class JupiterQuotes:
    def __init__(self, rps: float = 12.0, timeout: float = 8.0, fetch=None, min_rps: float = 2.0):
        """`rps` is a ceiling. The live pace adapts: it drops 30% on each 429 and creeps back up on successes
        (Jupiter's free tier throttled a steady 12 requests/s about 14% of the time)."""
        self.max_rps, self.min_rps, self.rps = rps, min_rps, rps
        self.timeout = timeout
        self._fetch = fetch or self._http
        self._next = 0.0
        self._pause_until = 0.0
        self._backoff = 1.0
        self._lock = asyncio.Lock()
        self.stats = {"requests": 0, "ok": 0, "no_route": 0, "throttled": 0, "errors": 0}

    @staticmethod
    def _http(url: str, timeout: float) -> tuple[int, dict]:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper-research/0.1", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return 0, {}

    async def _gate(self) -> None:
        """Global pacing: one request start per `min_gap`, and none while backing off after a 429/5xx."""
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next, self._pause_until)
            self._next = start + 1.0 / self.rps
            if start > now:
                await asyncio.sleep(start - now)

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> int | None:
        """outAmount in base units, or None when there is no route / the call failed."""
        await self._gate()
        qs = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint, "amount": amount, "slippageBps": 100})
        self.stats["requests"] += 1
        status, body = await asyncio.to_thread(self._fetch, f"{BASE}?{qs}", self.timeout)
        if status == 200 and "outAmount" in body:
            self.stats["ok"] += 1
            self._backoff = 1.0
            self.rps = min(self.max_rps, self.rps + 0.02)
            return int(body["outAmount"])
        if status == 429 or status >= 500 or status == 0:
            self.stats["throttled" if status == 429 else "errors"] += 1
            if status == 429:
                self.rps = max(self.min_rps, self.rps * 0.7)
            self._pause_until = time.monotonic() + min(self._backoff, 30.0)  # brief pause, doubling while it keeps failing
            self._backoff = min(self._backoff * 2, 30.0)
        else:
            self.stats["no_route"] += 1
        return None

    async def buy(self, mint: str, sol: float) -> float | None:
        out = await self.quote(WSOL, mint, int(sol * 1e9))
        return None if out is None else out / 1e6

    async def sell(self, mint: str, tokens: float) -> float | None:
        out = await self.quote(mint, WSOL, int(tokens * 1e6))
        return None if out is None else out / 1e9
