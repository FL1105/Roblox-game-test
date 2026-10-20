"""Join Kalshi markets, their candles and BTC spot into one row per market, as seen at the entry moment.

Causality: every field is read at or before the entry time (close - lead_s). The market's result is the only
thing that comes after, and it is only used to score the trade.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .api import Candle, Market

WINDOW_S = 900
MOM_MIN = 3
VOL_MIN = 60


@dataclass(frozen=True)
class Sample:
    ticker: str
    close_ts: int
    yes_won: bool
    bid: float                   # YES bid at entry, cents
    ask: float                   # YES ask at entry, cents
    bid_prev: float | None       # YES bid / ask MOM_MIN minutes earlier
    ask_prev: float | None
    tau_min: float               # minutes left at entry
    strike: float | None         # Kalshi's reference price (index based)
    spot_open: float | None      # BTC at the window start / MOM_MIN before entry / at entry / at close
    spot_prev: float | None
    spot_entry: float | None
    spot_end: float | None
    sigma: float | None          # per-minute log-return std over the VOL_MIN minutes before entry

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def build_samples(markets: list[Market], load_candles: Callable[[Market], list[Candle]], spot,
                  lead_s: int = 120, max_spread: float = 10.0) -> tuple[list[Sample], Counter]:
    """One Sample per usable market, plus counts of why the others were dropped.

    `spot` is a SpotSeries (or anything with .at(ts) and .minute_vol(ts, n)), or None to run without BTC data.
    """
    out: list[Sample] = []
    skipped: Counter = Counter()
    for m in markets:
        entry_ts = m.close_ts - lead_s
        by_end = {c.end_ts: c for c in load_candles(m)}
        now, before = by_end.get(entry_ts), by_end.get(entry_ts - 60 * MOM_MIN)
        if now is None:
            skipped["no candle at entry"] += 1
            continue
        if now.bid is None or now.ask is None:
            skipped["no two-sided quote"] += 1
            continue
        if not (0 <= now.bid <= now.ask <= 100):
            skipped["bad quote"] += 1
            continue
        if now.ask - now.bid > max_spread:
            skipped["spread too wide"] += 1
            continue
        start = m.close_ts - WINDOW_S
        spot_vals = (None,) * 5
        if spot is not None:
            spot_vals = (spot.at(start), spot.at(entry_ts - 60 * MOM_MIN), spot.at(entry_ts), spot.at(m.close_ts),
                         spot.minute_vol(entry_ts, VOL_MIN))
            if None in spot_vals:
                skipped["no BTC data (kept for Kalshi-only strategies)"] += 1
        out.append(Sample(
            m.ticker, m.close_ts, m.yes_won, now.bid, now.ask,
            before.bid if before else None, before.ask if before else None,
            (m.close_ts - entry_ts) / 60.0, m.strike, *spot_vals,
        ))
    return out, skipped
