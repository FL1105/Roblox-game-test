"""BTC/USD one-minute candles from Coinbase's public API, used as the underlying price for fair-value quoting."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .api import get_json

URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CACHE = Path("data/poly/btc_1m.json")


def fetch_range(start: int, end: int) -> dict[int, float]:
    """Open price of every one-minute candle from `start` to `end` (unix seconds), cached on disk."""
    have: dict[int, float] = {int(k): v for k, v in json.loads(CACHE.read_text()).items()} if CACHE.exists() else {}
    t = start // 60 * 60
    while t < end:
        stop = min(t + 300 * 60, end)
        if any((m not in have) for m in range(t, stop, 60)):
            for c in get_json(URL, {"granularity": 60, "start": _iso(t), "end": _iso(stop)}):
                have[int(c[0])] = float(c[3])  # [time, low, high, open, close, volume]
        t = stop
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(have))
    return have


def _iso(t: int) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Btc:
    """Price lookups that only use what was known at that moment: the open of the current minute."""

    def __init__(self, opens: dict[int, float]):
        self.opens = opens

    def price(self, t: float) -> float | None:
        return self.opens.get(int(t) // 60 * 60)

    def vol_per_min(self, t: float, window: int = 60) -> float:
        """Standard deviation of one-minute log returns over the previous `window` minutes."""
        m = int(t) // 60 * 60
        rets = []
        for i in range(window):
            a, b = self.opens.get(m - 60 * (i + 1)), self.opens.get(m - 60 * i)
            if a and b:
                rets.append(math.log(b / a))
        if len(rets) < 10:
            return 0.0006  # a typical value (0.06% a minute) when data is missing
        mean = sum(rets) / len(rets)
        return max((sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5, 0.0001)


def up_probability(spot: float, strike: float, seconds_left: float, vol_min: float) -> float:
    """Chance BTC finishes at or above the strike, treating it as a driftless random walk."""
    sd = vol_min * math.sqrt(max(seconds_left, 1.0) / 60.0)
    z = math.log(spot / strike) / sd
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
