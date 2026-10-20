"""BTC-USD 1-minute candles from Coinbase's public API, cached in aligned chunks.

Used for the trend and the gap-versus-volatility model. Kalshi itself prices from the CF Benchmarks index, so
there is a small constant offset to Coinbase; trend and gap are computed against Coinbase's own price at the
window start, which cancels it.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from .api import CACHE, get_json, read_cache, write_cache

BASE = "https://api.exchange.coinbase.com"
PRODUCT = "BTC-USD"
CHUNK_MIN = 300  # Coinbase returns at most 300 candles per request


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SpotSeries:
    """Minute candles keyed by the minute's start time: {start_ts: (open, close)}."""

    def __init__(self, minutes: dict[int, tuple[float, float]]):
        self.minutes = minutes

    @classmethod
    def fetch(cls, start_ts: int, end_ts: int, product: str = PRODUCT, progress=None) -> "SpotSeries":
        span = CHUNK_MIN * 60
        minutes: dict[int, tuple[float, float]] = {}
        first = start_ts // span * span
        chunks = range(first, end_ts, span)
        for i, chunk in enumerate(chunks, 1):
            path = CACHE / "spot" / f"{product}_{chunk}.json"
            rows = read_cache(path)
            if rows is None:
                rows = get_json(f"{BASE}/products/{product}/candles",
                                {"granularity": 60, "start": iso(chunk), "end": iso(chunk + span - 60)}, pause=0.12)
                if chunk + span < time.time() - 120:  # never cache a chunk that is still being written
                    write_cache(path, rows)
            for t, _low, _high, op, cl, _vol in rows:  # Coinbase row: [time, low, high, open, close, volume]
                minutes[int(t)] = (float(op), float(cl))
            if progress and i % 25 == 0:
                progress(i, len(chunks))
        return cls(minutes)

    def at(self, ts: int) -> float | None:
        """Price at a minute boundary: the close of the minute ending there, else the open of the one starting there."""
        prev = self.minutes.get(ts - 60)
        if prev:
            return prev[1]
        nxt = self.minutes.get(ts)
        return nxt[0] if nxt else None

    def minute_vol(self, end_ts: int, n: int = 60) -> float | None:
        """Per-minute standard deviation of log returns over the last n minutes up to a boundary, zero-mean.

        Gaps are allowed (a return across a gap is scaled by its length in minutes); needs 80% of the grid.
        """
        points = [(k, p) for k in range(n, -1, -1) if (p := self.at(end_ts - 60 * k)) is not None]
        if len(points) < 0.8 * n:
            return None
        var_sum = span = 0.0
        for (k0, p0), (k1, p1) in zip(points, points[1:]):
            var_sum += math.log(p1 / p0) ** 2
            span += k0 - k1
        return math.sqrt(var_sum / span) if span else None
