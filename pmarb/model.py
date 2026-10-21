"""Quotes, fees and the arithmetic of a hedged set of legs. All prices are dollars per $1-payout contract."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fee:
    """Taker fee = rate * qty * (p * (1 - p)) ** exponent, rounded up to the cent on the whole order when cent_round.

    Kalshi: rate 0.07 * the series multiplier, exponent 1, rounded up. Polymarket: the market's own feeSchedule
    (crypto 0.07, politics 0.04, geopolitics 0, ...), exponent 1, no rounding. Makers pay nothing on either.
    """
    rate: float = 0.0
    exponent: float = 1.0
    cent_round: bool = False

    def total(self, price: float, qty: float) -> float:
        raw = qty * self.rate * (price * (1.0 - price)) ** self.exponent
        return math.ceil(raw * 100.0 - 1e-9) / 100.0 if self.cent_round and raw > 0 else raw


@dataclass(frozen=True)
class Quote:
    """Top of book for the YES side of one binary market. NO is the complement: buy NO at 1 - bid, sell-side size = bid size."""
    venue: str
    event: str
    market: str
    title: str
    label: str                 # the outcome this market is about (candidate, threshold, ...)
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    fee: Fee
    min_size: float = 1.0
    close_ts: int | None = None
    strike_type: str = ""
    strike: float | None = None   # floor_strike for greater-type, cap_strike for less-type markets
    rules: str = ""
    yes_name: str = ""            # what YES means when it is a named outcome (a team, a candidate)
    kind: str = ""                # venue-specific market type, e.g. Polymarket's sportsMarketType

    @property
    def no_ask(self) -> float | None:
        return None if self.bid is None else 1.0 - self.bid


@dataclass(frozen=True)
class Leg:
    venue: str
    market: str
    label: str
    side: str       # "yes" | "no"
    price: float
    size: float
    fee: Fee
    min_size: float = 1.0


@dataclass(frozen=True)
class Opportunity:
    kind: str
    event: str
    title: str
    legs: tuple[Leg, ...]
    payout_min: float          # guaranteed payout per set, dollars
    qty: float                 # sets the top of book supports
    cost: float                # dollars for qty sets, before fees
    fees: float
    net: float                 # guaranteed profit for qty sets after fees (>0 is an arbitrage)
    risk_free: bool            # False when the guarantee rests on an unverified assumption
    note: str = ""

    @property
    def roi(self) -> float:
        return self.net / (self.cost + self.fees) if self.cost else 0.0


def build(kind: str, event: str, title: str, legs: list[Leg], payout_min: float, risk_free: bool, note: str = "",
          max_qty: float = 1000.0) -> Opportunity | None:
    """Size a set of legs to what the top of book supports and price it; None if it cannot be filled at all."""
    qty = math.floor(min([max_qty] + [leg.size for leg in legs]))
    if qty < 1 or qty < max(leg.min_size for leg in legs):
        return None
    cost = qty * sum(leg.price for leg in legs)
    fees = sum(leg.fee.total(leg.price, qty) for leg in legs)
    return Opportunity(kind, event, title, tuple(legs), payout_min, qty, cost, fees, qty * payout_min - cost - fees, risk_free, note)


def yes_leg(q: Quote) -> Leg | None:
    return None if q.ask is None or not 0 < q.ask < 1 else Leg(q.venue, q.market, q.label, "yes", q.ask, q.ask_size, q.fee, q.min_size)


def no_leg(q: Quote) -> Leg | None:
    return None if q.bid is None or not 0 < q.bid < 1 else Leg(q.venue, q.market, q.label, "no", 1.0 - q.bid, q.bid_size, q.fee, q.min_size)
