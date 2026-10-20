"""The strategies, the fee, and the fill. Every strategy sees one Sample and answers "yes", "no" or None (skip)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .data import Sample
from .stats import norm_cdf

FEE_RATE = 0.07


@dataclass(frozen=True)
class Config:
    contracts: int = 10          # order size; the fee is rounded up to the cent on the whole order
    fee_multiplier: float = 1.0  # from the series object; scales FEE_RATE
    edge_cents: float = 3.0      # model_edge: minimum (model probability - price - fee)
    settle_avg_min: float = 1.0  # the result is the average of the index over the last this-many minutes


@dataclass(frozen=True)
class Trade:
    ticker: str
    close_ts: int
    side: str
    price: float  # cents per contract
    fee: float    # cents per contract
    win: bool
    pnl: float    # cents per contract


def fee_cents(price: float, contracts: int, multiplier: float = 1.0) -> float:
    """Kalshi taker fee per contract in cents: ceil(0.07 * mult * C * P * (1 - P)) cents on the order, split over C."""
    p = price / 100.0
    total = math.ceil(FEE_RATE * multiplier * contracts * p * (1.0 - p) * 100.0 - 1e-9)
    return total / contracts


def side_price(s: Sample, side: str) -> float:
    return s.ask if side == "yes" else 100.0 - s.bid


def effective_minutes(tau_min: float, settle_avg_min: float) -> float:
    """Variance-equivalent time left when the result is the mean of the last L minutes of a random walk.

    The mean of a Brownian path over [tau - L, tau] has variance sigma^2 * (tau - 2L/3).
    """
    return tau_min - 2.0 * settle_avg_min / 3.0


def model_p_up(s: Sample, cfg: Config) -> float | None:
    """Probability that the settlement average ends at or above the window start, given the gap now."""
    if s.spot_open is None or s.spot_entry is None or not s.sigma:
        return None
    t_eff = effective_minutes(s.tau_min, cfg.settle_avg_min)
    if t_eff <= 0:
        return None
    gap = math.log(s.spot_entry / s.spot_open)
    return norm_cdf(gap / (s.sigma * math.sqrt(t_eff)))


def _sign(x: float) -> str | None:
    return None if x == 0 else ("yes" if x > 0 else "no")


def trend_open(s: Sample, cfg: Config) -> str | None:
    if s.spot_open is None or s.spot_entry is None:
        return None
    return _sign(s.spot_entry - s.spot_open)


def trend_mom3(s: Sample, cfg: Config) -> str | None:
    if s.spot_prev is None or s.spot_entry is None:
        return None
    return _sign(s.spot_entry - s.spot_prev)


def trend_kalshi(s: Sample, cfg: Config) -> str | None:
    if s.bid_prev is None or s.ask_prev is None:
        return None
    return _sign(s.mid - (s.bid_prev + s.ask_prev) / 2.0)


def favorite(s: Sample, cfg: Config) -> str | None:
    return _sign(s.mid - 50.0)


def model_edge(s: Sample, cfg: Config) -> str | None:
    p = model_p_up(s, cfg)
    if p is None:
        return None
    best_side, best_edge = None, cfg.edge_cents
    for side, prob in (("yes", p), ("no", 1.0 - p)):
        price = side_price(s, side)
        if not 0 < price < 100:
            continue
        edge = prob * 100.0 - price - fee_cents(price, cfg.contracts, cfg.fee_multiplier)
        if edge >= best_edge:
            best_side, best_edge = side, edge
    return best_side


def always_yes(s: Sample, cfg: Config) -> str | None:
    return "yes"


def always_no(s: Sample, cfg: Config) -> str | None:
    return "no"


PREREGISTERED: dict[str, Callable[[Sample, Config], str | None]] = {
    "trend_open": trend_open,
    "trend_mom3": trend_mom3,
    "trend_kalshi": trend_kalshi,
    "model_edge": model_edge,
    "favorite": favorite,
}
CONTROLS: dict[str, Callable[[Sample, Config], str | None]] = {"always_yes": always_yes, "always_no": always_no}


def take(s: Sample, side: str, cfg: Config) -> Trade | None:
    """Buy `side` at the quoted ask; None when that side has no tradable price (0 or 100)."""
    price = side_price(s, side)
    if not 0 < price < 100:
        return None
    fee = fee_cents(price, cfg.contracts, cfg.fee_multiplier)
    win = s.yes_won == (side == "yes")
    return Trade(s.ticker, s.close_ts, side, price, fee, win, (100.0 if win else 0.0) - price - fee)


def run(strategy: Callable[[Sample, Config], str | None], samples: list[Sample], cfg: Config) -> list[Trade]:
    """Trades in time order."""
    trades = []
    for s in sorted(samples, key=lambda x: x.close_ts):
        side = strategy(s, cfg)
        if side is not None and (t := take(s, side, cfg)) is not None:
            trades.append(t)
    return trades
