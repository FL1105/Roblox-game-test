"""Single-venue checks, which carry no settlement-source mismatch.

Kalshi: for strikes k1 < k2, Yes(k1) pays whenever Yes(k2) does, so holding Yes(k1) and No(k2) pays at least $1 ($2 between the strikes).
Polymarket: Up and Down together always pay exactly $1. If either bundle costs under $1 after fees it is a risk-free profit."""

from __future__ import annotations

from dataclasses import dataclass

from .arb import kalshi_fee, poly_fee, share_grid


@dataclass(frozen=True)
class LadderArb:
    low: float  # strike of the Yes leg
    high: float  # strike of the No leg
    yes_ask: float
    no_ask: float
    contracts: float  # how many the top of book supports
    fees: float

    @property
    def cost(self) -> float:
        return self.contracts * (self.yes_ask + self.no_ask) + self.fees

    @property
    def profit(self) -> float:
        return self.contracts - self.cost

    @property
    def per_contract(self) -> float:
        return self.profit / self.contracts if self.contracts else 0.0


def ladder_arbs(rows: list[dict]) -> list[LadderArb]:
    """Every strike pair (k1 <= k2) at top of book, best first. `rows` are Kalshi market objects with yes/no asks and bid sizes."""
    side = []
    for r in rows:
        try:
            k = float(r["floor_strike"]) + 0.01
            ya, na = float(r["yes_ask_dollars"]), float(r["no_ask_dollars"])
            ya_size, na_size = float(r.get("yes_ask_size_fp") or 0), float(r.get("yes_bid_size_fp") or 0)  # a No ask is a Yes bid
        except (KeyError, TypeError, ValueError):
            continue
        side.append((k, ya, ya_size, na, na_size))
    side.sort()
    out = []
    for i, (k1, ya1, ys1, _, _) in enumerate(side):
        if ya1 <= 0 or ya1 >= 1 or ys1 <= 0:
            continue
        for k2, _, _, na2, ns2 in side[i:]:
            if na2 <= 0 or na2 >= 1 or ns2 <= 0 or ya1 + na2 >= 1.0:
                continue  # at or above $1 before fees: cannot work
            n = min(ys1, ns2)
            fees = kalshi_fee(ya1, n) + kalshi_fee(na2, n)
            a = LadderArb(k1, k2, ya1, na2, n, fees)
            if a.profit > 0:
                out.append(a)
    return sorted(out, key=lambda a: -a.profit)


def poly_set(up_asks, down_asks, max_shares: float = 2000.0) -> tuple[float, float, float] | None:
    """(profit, shares, cost per share) of buying Up and Down together at the best size, or None if there is no depth."""
    if not up_asks or not down_asks:
        return None
    best = None
    cap = min(max_shares, sum(s for _, s in up_asks), sum(s for _, s in down_asks))
    for n in share_grid(cap):
        cost = fees = 0.0
        for asks in (up_asks, down_asks):
            left = n
            for p, s in asks:
                take = min(left, s)
                cost += take * p
                fees += poly_fee(p, take)
                left -= take
                if left <= 0:
                    break
        profit = n - cost - fees
        if best is None or profit > best[0]:
            best = (profit, n, (cost + fees) / n)
    return best
