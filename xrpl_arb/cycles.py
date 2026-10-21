"""Finding XRP -> ... -> XRP cycles and the input size that maximises their profit.

Every leg's output is a concave, increasing function of its input (better prices are always taken
first), and a composition of those is concave, so profit(x) = out(x) - x is concave with profit(0) = 0.
Two consequences used below: a cycle can only be profitable if the product of its legs' spot rates
exceeds 1, and the best size can be found by golden-section search.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Mapping

from .liquidity import ONE, ZERO, Market
from .model import ONE_DROP, XRP, Asset, floor_drop

GOLDEN = (Decimal(5).sqrt() - 1) / 2


@dataclass(frozen=True)
class Leg:
    asset_in: Asset
    asset_out: Asset
    amount_in: Decimal
    amount_out: Decimal


@dataclass(frozen=True)
class Opportunity:
    legs: tuple[Leg, ...]
    amount_in: Decimal
    amount_out: Decimal
    tx_cost: Decimal

    @property
    def profit(self) -> Decimal:
        """XRP gained after the network fee."""
        return self.amount_out - self.amount_in - self.tx_cost

    @property
    def profit_pct(self) -> Decimal:
        return self.profit / self.amount_in * 100

    @property
    def path(self) -> list[str]:
        return [str(leg.asset_in) for leg in self.legs] + [str(self.legs[-1].asset_out)]

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "amount_in": str(self.amount_in),
            "amount_out": str(self.amount_out),
            "tx_cost": str(self.tx_cost),
            "profit": str(self.profit),
            "profit_pct": float(self.profit_pct),
            "legs": [{"in": str(l.asset_in), "out": str(l.asset_out), "amount_in": str(l.amount_in),
                      "amount_out": str(l.amount_out)} for l in self.legs],
        }


def find_cycles(markets: Mapping[tuple[Asset, Asset], Market], base: Asset = XRP, max_legs: int = 4) -> list[tuple[Market, ...]]:
    """Every simple cycle from `base` back to `base` with 2..max_legs legs, over markets that have liquidity."""
    edges: dict[Asset, list[Asset]] = defaultdict(list)
    for (a, b), m in markets.items():
        if not m.empty:
            edges[a].append(b)
    cycles: list[tuple[Market, ...]] = []

    def walk(visited: list[Asset], legs: list[Market]) -> None:
        for nxt in edges[visited[-1]]:
            m = markets[(visited[-1], nxt)]
            if nxt == base:
                cycles.append(tuple(legs + [m]))
            elif nxt not in visited and len(legs) + 1 < max_legs:
                walk(visited + [nxt], legs + [m])

    walk([base], [])
    return cycles


def run_cycle(cycle: tuple[Market, ...], amount_in: Decimal, legs: list[Leg] | None = None, whole_drops: bool = True) -> Decimal:
    """Amount of the base asset that comes back from spending `amount_in` around the cycle.

    XRP amounts are rounded down to whole drops unless `whole_drops` is False (the size search turns it
    off so it climbs a smooth curve instead of one with drop-sized steps).
    """
    amt = amount_in
    pools: dict = {}  # pool state after earlier legs, for cycles that hit one pool twice
    for m in cycle:
        out, pool_after = m.quote(amt, pools.get(m.pool_key))
        if pool_after is not None:
            pools[m.pool_key] = pool_after
        if whole_drops and m.asset_out.is_xrp:
            out = floor_drop(out)
        if legs is not None:
            legs.append(Leg(m.asset_in, m.asset_out, amt, out))
        amt = out
    return amt


def best_size(cycle: tuple[Market, ...], max_in: Decimal) -> Decimal:
    """Input (whole drops, <= max_in) that maximises profit; 0 if no size is profitable."""
    def profit(x: Decimal) -> Decimal:
        return run_cycle(cycle, x, whole_drops=False) - x

    a, b = ZERO, max_in
    c, d = b - GOLDEN * (b - a), a + GOLDEN * (b - a)
    fc, fd = profit(c), profit(d)
    for _ in range(200):
        if b - a <= ONE_DROP:
            break
        if fc < fd:
            a, c, fc = c, d, fd
            d = a + GOLDEN * (b - a)
            fd = profit(d)
        else:
            b, d, fd = d, c, fc
            c = b - GOLDEN * (b - a)
            fc = profit(c)
    x = floor_drop((a + b) / 2)
    return x if x > 0 and run_cycle(cycle, x) - x > 0 else ZERO


def closest(markets: Mapping[tuple[Asset, Asset], Market], max_legs: int = 4) -> tuple[Decimal, tuple[Market, ...]] | None:
    """The cycle nearest to profitable: its return at vanishing size, as a percentage (-0.3 means it loses 0.3%
    before the network fee), and the cycle. This is the best any trade size could do, so a negative number
    means no size pays. None if the markets contain no cycle."""
    best = None
    with localcontext() as ctx:
        ctx.prec = 40
        for cycle in find_cycles(markets, XRP, max_legs):
            rate = ONE
            for m in cycle:
                rate *= m.spot_rate()
            if best is None or rate > best[0]:
                best = (rate, cycle)
    return None if best is None else ((best[0] - ONE) * 100, best[1])


def candidates(markets: Mapping[tuple[Asset, Asset], Market], *, max_xrp: Decimal, tx_cost: Decimal,
               max_legs: int = 4) -> list[Opportunity]:
    """Every cycle that returns more than it costs at its best size (before any threshold), best net profit first.
    `profit` is net of `tx_cost`, so a cycle that gains less than the fee shows a negative profit."""
    found: list[Opportunity] = []
    with localcontext() as ctx:
        ctx.prec = 40
        for cycle in find_cycles(markets, XRP, max_legs):
            rate = ONE
            for m in cycle:
                rate *= m.spot_rate()
            if rate <= ONE:  # profit is concave with slope rate-1 at 0, so nothing to find
                continue
            x = best_size(cycle, max_xrp)
            if x <= 0:
                continue
            legs: list[Leg] = []
            out = run_cycle(cycle, x, legs)
            found.append(Opportunity(tuple(legs), x, out, tx_cost))
    found.sort(key=lambda o: o.profit, reverse=True)
    return found


def passes(opp: Opportunity, min_profit: Decimal, min_profit_pct: Decimal) -> bool:
    return opp.profit >= min_profit and opp.profit_pct >= min_profit_pct


def scan(markets: Mapping[tuple[Asset, Asset], Market], *, max_xrp: Decimal, tx_cost: Decimal,
         min_profit: Decimal, min_profit_pct: Decimal, max_legs: int = 4) -> list[Opportunity]:
    """Profitable cycles, best first. Profit is net of `tx_cost`; both thresholds must be met."""
    return [o for o in candidates(markets, max_xrp=max_xrp, tx_cost=tx_cost, max_legs=max_legs)
            if passes(o, min_profit, min_profit_pct)]
