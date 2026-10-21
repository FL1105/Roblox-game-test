"""Cycles through the watched markets that start and end in the base asset, and the trade size that maximises each one's profit."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .assets import Asset
from .liquidity import Hop, Quote

GOLDEN = 0.6180339887498949


@dataclass(frozen=True)
class Route:
    assets: tuple[Asset, ...]  # base, ..., base

    @property
    def hops(self) -> int:
        return len(self.assets) - 1

    @property
    def id(self) -> str:
        return ">".join(a.key for a in self.assets)

    def label(self, name: Callable[[Asset], str]) -> str:
        return " → ".join(name(a) for a in self.assets)


@dataclass
class Leg:
    src: Asset
    dst: Asset
    amount_in: float
    amount_out: float
    amm_share: float


@dataclass
class Evaluation:
    route: Route
    size: float  # base spent
    out: float  # base received
    legs: list[Leg] = field(default_factory=list)
    fee_cost: float = 0.0  # network fees, in base
    complete: bool = True  # False when some hop ran out of liquidity

    @property
    def gross(self) -> float:
        return self.out - self.size

    @property
    def profit(self) -> float:
        return self.out - self.size - self.fee_cost

    @property
    def edge_bps(self) -> float:
        return self.profit / self.size * 1e4 if self.size > 0 else 0.0


def find_routes(base: Asset, markets: list[tuple[Asset, Asset]], max_hops: int = 3) -> list[Route]:
    """Every simple cycle base -> ... -> base of 2..max_hops hops over the markets (each market is tradable both ways). A 2-hop
    cycle goes out and back over the same market; longer cycles never revisit an asset."""
    nbrs: dict[str, list[Asset]] = {}
    for a, b in markets:
        nbrs.setdefault(a.key, []).append(b)
        nbrs.setdefault(b.key, []).append(a)
    routes: list[Route] = []

    def walk(path: list[Asset]) -> None:
        here = path[-1]
        for nxt in nbrs.get(here.key, []):
            if nxt == base:
                if len(path) >= 2:
                    routes.append(Route(tuple(path + [base])))
            elif nxt not in path and len(path) < max_hops:
                walk(path + [nxt])

    walk([base])
    seen, out = set(), []
    for r in routes:
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return sorted(out, key=lambda r: (r.hops, r.id))


def evaluate(route: Route, hops: list[Hop], pools: dict[str, list[float]], size: float, fee_cost: float = 0.0) -> Evaluation:
    """Push `size` of base around the cycle. `pools` is scratch state (copy it first)."""
    amount = size
    legs: list[Leg] = []
    complete = True
    for hop in hops:
        q: Quote = hop.quote(amount, pools)
        if q.exhausted or q.spent < amount * (1 - 1e-9):
            complete = False
        legs.append(Leg(hop.src, hop.dst, q.spent, q.out, q.amm_share))
        amount = q.out
    return Evaluation(route, size, amount, legs, fee_cost, complete)


def optimize(route: Route, hops: list[Hop], fresh_pools: Callable[[], dict[str, list[float]]], lo: float, hi: float,
             fee_cost: float = 0.0, iters: int = 48) -> Evaluation:
    """Size in [lo, hi] with the largest profit. Profit is concave in size (every hop is), so golden-section search finds it.
    When no size makes money this returns the evaluation at `lo`, which shows how far under water the route is."""

    def at(size: float) -> Evaluation:
        return evaluate(route, hops, fresh_pools(), size, fee_cost)

    best_lo = at(lo)
    if hi <= lo:
        return best_lo
    a, b = lo, hi
    c, d = b - GOLDEN * (b - a), a + GOLDEN * (b - a)
    fc, fd = at(c), at(d)
    for _ in range(iters):
        if fc.profit < fd.profit:
            a, c, fc = c, d, fd
            d = a + GOLDEN * (b - a)
            fd = at(d)
        else:
            b, d, fd = d, c, fc
            c = b - GOLDEN * (b - a)
            fc = at(c)
    best = max((best_lo, fc, fd, at(hi)), key=lambda e: e.profit)
    return best
