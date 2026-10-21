"""Liquidity model for one hop (pay asset `src`, receive asset `dst`): the order book and the AMM pool of the pair, merged the way
the ledger merges them.

A payment through a pair takes whichever is cheaper at the margin. The AMM is used until its post-trade price equals the next
offer's price, then that offer is taken, and so on. The pool keeps its fee (reserves grow by the full input), so a trade split
across several steps is priced from the pool state after the previous step."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .assets import Asset

EPS = 1e-12


@dataclass(frozen=True)
class Offer:
    inp: float  # what the taker pays, in the hop's src asset
    out: float  # what the taker receives, in the hop's dst asset
    owner: str = ""

    @property
    def price(self) -> float:  # src paid per dst received
        return self.inp / self.out


@dataclass(frozen=True)
class Pool:
    a: Asset
    b: Asset
    ra: float
    rb: float
    fee: float  # fraction, e.g. 0.00205 for the 205/100000 the ledger reports

    @property
    def key(self) -> str:
        return pool_key(self.a, self.b)


def pool_key(x: Asset, y: Asset) -> str:
    return "|".join(sorted((x.key, y.key)))


@dataclass
class Quote:
    spent: float = 0.0  # src actually consumed (can be below the budget when liquidity runs out)
    out: float = 0.0
    amm_in: float = 0.0
    amm_out: float = 0.0
    clob_in: float = 0.0
    clob_out: float = 0.0
    exhausted: bool = False

    @property
    def amm_share(self) -> float:
        total = self.amm_in + self.clob_in
        return self.amm_in / total if total > 0 else 0.0


def amm_out(x: float, y: float, fee: float, amount_in: float) -> float:
    """Output of one swap against reserves (x = reserve of the asset paid in, y = reserve of the asset paid out)."""
    return y * amount_in * (1 - fee) / (x + amount_in * (1 - fee))


def amm_take_to_price(x: float, y: float, fee: float, price: float) -> float:
    """Gross input that moves the pool to a post-trade marginal price of `price` (src per dst), or 0 when the pool already
    quotes worse than that. Solves (x + t)(x + (1-f)t) = price (1-f) x y."""
    f1 = 1 - fee
    c = x * x - price * f1 * x * y
    if c >= 0:
        return 0.0
    b = (1 + f1) * x
    return (-b + math.sqrt(b * b - 4 * f1 * c)) / (2 * f1)


class Hop:
    """Merged order book + AMM for src -> dst. `pool` reserves are read from / written to the caller's scratch dict, so a route
    that touches the same pool twice sees its own earlier trade."""

    def __init__(self, src: Asset, dst: Asset, offers: Sequence[Offer], pool: Pool | None, rate_in: float = 1.0, rate_out: float = 1.0):
        # The payer bears the issuer's transfer fee on the issued currency sent AND on the one received (checked against the
        # ledger's own simulation: delivered = raw output / rate_out, for pool and book alike).
        self.src, self.dst, self.pool, self.rate_in, self.rate_out = src, dst, pool, rate_in, rate_out
        self.offers = [o for o in offers if o.out > EPS and o.inp > EPS]

    def spot(self, pools: dict[str, list[float]] | None = None) -> float | None:
        """Best marginal price (src per dst) across both venues, ignoring transfer fees."""
        best = self.offers[0].price if self.offers else None
        if self.pool:
            x, y = self._xy(pools)
            p = x / ((1 - self.pool.fee) * y)
            best = p if best is None else min(best, p)
        return best

    def _xy(self, pools) -> tuple[float, float]:
        ra, rb = (pools[self.pool.key] if pools is not None else (self.pool.ra, self.pool.rb))
        return (ra, rb) if self.pool.a == self.src else (rb, ra)

    def quote(self, amount_in: float, pools: dict[str, list[float]] | None = None) -> Quote:
        """Trade `amount_in` of src (transfer fees included on both sides). Mutates `pools` if given."""
        q = Quote()
        rem = amount_in / self.rate_in
        if rem <= 0:
            return q
        pool = self.pool
        if pool:
            x, y = self._xy(pools)
            fee = pool.fee
        i, taken = 0, 0.0  # next offer, and how much of it is already consumed
        while rem > EPS * amount_in:
            offer = self.offers[i] if i < len(self.offers) else None
            if pool:
                if offer is None:
                    t = rem
                else:
                    t = min(rem, amm_take_to_price(x, y, fee, offer.price))
                if t > 0:
                    dy = amm_out(x, y, fee, t)
                    x, y = x + t, y - dy
                    rem -= t
                    q.amm_in += t
                    q.amm_out += dy
                    q.out += dy
                    if rem <= EPS * amount_in:
                        break
            if offer is None:
                q.exhausted = True  # nothing left to trade against (an AMM alone never runs dry; this means no AMM)
                break
            avail = offer.out - taken
            want = rem / offer.price
            if want < avail:
                q.clob_in += rem
                q.clob_out += want
                q.out += want
                rem = 0.0
                break
            q.clob_in += avail * offer.price
            q.clob_out += avail
            q.out += avail
            rem -= avail * offer.price
            i, taken = i + 1, 0.0
        q.spent = (amount_in / self.rate_in - max(rem, 0.0)) * self.rate_in
        if self.rate_out != 1.0:
            q.out, q.amm_out, q.clob_out = q.out / self.rate_out, q.amm_out / self.rate_out, q.clob_out / self.rate_out
        if pool and pools is not None:
            ra, rb = (x, y) if pool.a == self.src else (y, x)
            pools[pool.key] = [ra, rb]
        return q
