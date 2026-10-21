"""Quoting a swap against an XRPL order book and an AMM pool together.

A `Market` is one direction of a pair (spend `asset_in`, receive `asset_out`). It holds the order-book
offers that sell `asset_out` for `asset_in`, and the pair's XLS-30 AMM pool if there is one. Where both
exist the quote splits the input the way the ledger's payment engine does: take whichever has the better
marginal price, moving the AMM only until its marginal price meets the next offer.

Assumptions to verify against the live ledger (they are the places a model like this can be off):
- AMM: constant product, fee kept in the pool (`out = y*m*dx / (x + m*dx)`, `m = 1 - fee`), full `dx` added to the reserve.
- Routing: the AMM is swapped only far enough that its spot price meets the next offer, then that offer is taken,
  and so on (the ledger builds an AMM offer sized to the order book's quality). Each piece pays the fee again, so with
  a fee this lands up to ~0.03% of output under a single-shot optimal split (measured on random books); with no fee it is exact.
- Transfer fees: charged on the issued currency we spend, at the issuer's rate, for books and pools alike.
  That is the conservative reading; if pools turn out to be exempt this under-reports some profit, never over-reports.
- Offers are taken at their stated price with no rounding beyond Decimal precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .model import Asset

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class Offer:
    pays: Decimal  # what the taker sends for the whole offer (asset_in units)
    gets: Decimal  # what the taker receives for the whole offer (asset_out units)

    @property
    def price(self) -> Decimal:
        """asset_in per asset_out; lower is better for the taker."""
        return self.pays / self.gets


@dataclass(frozen=True)
class Pool:
    a: Asset
    b: Asset
    ra: Decimal
    rb: Decimal
    fee: Decimal  # fraction, e.g. 0.003

    @property
    def key(self) -> frozenset:
        return frozenset((self.a, self.b))

    def reserves(self, asset_in: Asset) -> tuple[Decimal, Decimal]:
        """(reserve of the asset going in, reserve of the asset coming out)."""
        return (self.ra, self.rb) if asset_in == self.a else (self.rb, self.ra)

    def swap(self, asset_in: Asset, dx: Decimal) -> tuple[Decimal, "Pool"]:
        """Output for `dx` in, and the pool afterwards."""
        x, y = self.reserves(asset_in)
        m = ONE - self.fee
        dy = y * m * dx / (x + m * dx)
        x, y = x + dx, y - dy
        return dy, (Pool(self.a, self.b, x, y, self.fee) if asset_in == self.a else Pool(self.a, self.b, y, x, self.fee))


def _amm_in_to_price(x: Decimal, y: Decimal, m: Decimal, price: Decimal) -> Decimal:
    """Input after which the pool's spot price (in per out) equals `price`; 0 if it is already there.

    The fee stays in the reserve, so after a swap of dx the pool holds (x + dx, x*y / (x + m*dx)) and its spot
    price is (x + dx)(x + m*dx) / (m*y*x). Setting that to `price` gives a quadratic in dx:
    m*dx^2 + (1 + m)*x*dx + x^2 - price*m*y*x = 0.
    """
    if price * m * y <= x:  # spot price (x / (m*y)) is already at or above `price`
        return ZERO
    root = ((1 - m) ** 2 * x * x + 4 * m * m * price * x * y).sqrt()
    return (root - (1 + m) * x) / (2 * m)


@dataclass(frozen=True)
class Market:
    asset_in: Asset
    asset_out: Asset
    offers: tuple[Offer, ...] = ()  # best price first
    pool: Pool | None = None
    in_rate: Decimal = ONE  # issuer transfer rate on asset_in (1.005 = 0.5%)

    @property
    def pool_key(self) -> frozenset:
        return frozenset((self.asset_in, self.asset_out))

    @property
    def empty(self) -> bool:
        return not self.offers and self.pool is None

    def spot_rate(self) -> Decimal:
        """Output per unit spent at vanishing size (best of book and pool, after the transfer fee)."""
        best = ZERO
        if self.offers:
            best = ONE / self.offers[0].price
        if self.pool is not None:
            x, y = self.pool.reserves(self.asset_in)
            best = max(best, y * (ONE - self.pool.fee) / x)
        return best / self.in_rate

    def quote(self, gross_in: Decimal, pool: Pool | None = None) -> tuple[Decimal, Pool | None]:
        """Output for spending `gross_in`, and the pool afterwards. `pool` overrides the stored one
        (a cycle that touches the same pool twice must see the first trade's effect)."""
        pool = self.pool if pool is None else pool
        remaining = gross_in / self.in_rate
        out = ZERO
        if remaining <= 0:
            return out, pool
        for offer in self.offers:
            if pool is not None:
                x, y = pool.reserves(self.asset_in)
                dx = min(_amm_in_to_price(x, y, ONE - pool.fee, offer.price), remaining)
                if dx > 0:
                    dy, pool = pool.swap(self.asset_in, dx)
                    out += dy
                    remaining -= dx
                    if remaining <= 0:
                        return out, pool
            take = min(offer.pays, remaining)
            out += offer.gets * take / offer.pays
            remaining -= take
            if remaining <= 0:
                return out, pool
        if pool is not None:
            dy, pool = pool.swap(self.asset_in, remaining)
            out += dy
        return out, pool
