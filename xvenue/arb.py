"""The arithmetic of a cross-venue hedge: what a set of shares costs across two order books, after fees, against a payout of $1.

Polymarket's hourly BTC market pays $1 for Up if the Binance candle closes at or above its open (the 'price to beat') and for Down
otherwise. Kalshi's hourly ladder pays $1 for Yes if BRTI's last-minute average is above the strike. Holding Polymarket Up and Kalshi No
at a strike K >= Kp always pays at least $1 (Up if the price ends >= Kp, No if it ends < K), and $2 when it ends in [Kp, K); holding
Polymarket Down and Kalshi Yes at K <= Kp is the mirror. If the two venues measured the same price, anything that costs less than $1 all
in would be risk-free. The two venues use different price sources, so the guarantee holds only when the strike gap exceeds the difference
between those sources: see `basis_ok`."""

from __future__ import annotations

import math
from dataclasses import dataclass

POLY_FEE_RATE = 0.07  # taker: fee = shares * rate * p * (1 - p); makers pay nothing
KALSHI_FEE_RATE = 0.07  # quadratic schedule, multiplier 1: fee = ceil(rate * shares * p * (1 - p)) to the cent (coefficient assumed, see README)

Level = tuple[float, float]  # (price, shares), best first


def poly_fee(price: float, shares: float) -> float:
    return shares * POLY_FEE_RATE * price * (1 - price)


def kalshi_fee(price: float, shares: float) -> float:
    return math.ceil(KALSHI_FEE_RATE * shares * price * (1 - price) * 100 - 1e-9) / 100


def sweep(asks: list[Level], shares: float, fee) -> tuple[float, float, float]:
    """Cost of buying up to `shares` from the asks. Returns (cost without fees, fees, shares actually filled)."""
    cost = fees = filled = 0.0
    for price, size in asks:
        take = min(size, shares - filled)
        if take <= 0:
            break
        cost += take * price
        fees += fee(price, take)
        filled += take
    return cost, fees, filled


def share_grid(cap: float) -> list[float]:
    """Sizes to try: every share up to 50, then 15% steps, and always the full depth `cap`."""
    out, n = [], 1.0
    while n < cap:
        out.append(n)
        n = n + 1 if n < 50 else n * 1.15
    return out + [cap] if cap >= 1 else []


@dataclass(frozen=True)
class Hedge:
    poly_side: str  # "Up" or "Down"
    kalshi_side: str  # "Yes" or "No"
    strike: float  # Kalshi strike
    gap: float  # |Kalshi strike - Polymarket strike|, dollars: the band that pays $2 and the room for source differences
    shares: float
    cost: float  # what the shares cost, fees excluded
    fees: float
    top_sum: float  # best ask on each side added up, the number a naive detector looks at

    @property
    def profit(self) -> float:
        """Worst-case profit if both venues settle on the same price: payout of at least $1 a share minus cost and fees."""
        return self.shares - self.cost - self.fees

    @property
    def per_share(self) -> float:
        return self.profit / self.shares if self.shares else 0.0


def hedge_sides(poly_strike: float, kalshi_strike: float) -> tuple[str, str]:
    """Which pair of positions covers every outcome: Up + No when the Kalshi strike is at or above Polymarket's, else Down + Yes."""
    return ("Up", "No") if kalshi_strike >= poly_strike else ("Down", "Yes")


POLY_MIN_SHARES = 5.0  # Polymarket's minimum order size on these markets


def best_hedge(poly_strike: float, kalshi_strike: float, poly_asks: list[Level], kalshi_asks: list[Level], max_shares: float = 2000.0,
               budget: float | None = None, min_shares: float = POLY_MIN_SHARES) -> Hedge | None:
    """The share count that maximises worst-case profit for one strike, given both ask books (already for the right sides).
    `budget` caps what the shares may cost including fees (the bankroll); `min_shares` is the smallest order the venues accept."""
    ps, ks = hedge_sides(poly_strike, kalshi_strike)
    if not poly_asks or not kalshi_asks:
        return None
    top = poly_asks[0][0] + kalshi_asks[0][0]
    cap = min(max_shares, sum(s for _, s in poly_asks), sum(s for _, s in kalshi_asks))
    best: Hedge | None = None
    for n in share_grid(cap):
        if n < min_shares:
            continue
        pc, pf, pn = sweep(poly_asks, n, poly_fee)
        kc, kf, kn = sweep(kalshi_asks, n, kalshi_fee)
        if min(pn, kn) < n - 1e-9 or (budget is not None and pc + kc + pf + kf > budget):
            break
        h = Hedge(ps, ks, kalshi_strike, abs(kalshi_strike - poly_strike), n, pc + kc, pf + kf, top)
        if best is None or h.profit > best.profit:
            best = h
    if best is None:  # even one share was not available on both sides
        return None
    return best


def basis_ok(gap: float, source_difference: float) -> bool:
    """The hedge cannot lose both legs unless the sources disagree by more than the strike gap."""
    return gap > source_difference
