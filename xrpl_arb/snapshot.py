"""Reading the ledger into `Market` objects."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from .client import XrplClient, XrplError
from .liquidity import ONE, Market, Offer, Pool
from .model import XRP, Asset, amount_asset, amount_value

FEE_UNITS = Decimal(100_000)  # AMM trading_fee is in 1/100000


@dataclass
class Snapshot:
    markets: dict[tuple[Asset, Asset], Market]
    ledger: int  # the validated ledger every read was pinned to
    consistent: bool  # False if a read had to fall back to a newer ledger
    books: int
    pools: int
    skipped: list[str]  # reads whose response couldn't be understood; the pair is left out


class RateCache:
    """Issuer transfer rates change rarely; ask once an hour."""

    def __init__(self, client: XrplClient, ttl: float = 3600.0):
        self.client, self.ttl = client, ttl
        self._rates: dict[str, tuple[float, Decimal]] = {}

    def get(self, asset: Asset) -> Decimal:
        if asset.is_xrp:
            return ONE
        hit = self._rates.get(asset.issuer)
        if hit is None or time.time() - hit[0] > self.ttl:
            hit = (time.time(), self.client.transfer_rate(asset.issuer))
            self._rates[asset.issuer] = hit
        return hit[1]


def parse_offers(result: dict) -> tuple[Offer, ...]:
    """Offers from a book_offers result, using the funded amounts when the owner can't cover the whole offer."""
    offers = []
    for o in result.get("offers", []):
        gets_full, pays_full = amount_value(o["TakerGets"]), amount_value(o["TakerPays"])
        if gets_full <= 0:
            continue
        gets = amount_value(o["taker_gets_funded"]) if "taker_gets_funded" in o else gets_full
        pays = amount_value(o["taker_pays_funded"]) if "taker_pays_funded" in o else pays_full * gets / gets_full
        if gets > 0 and pays > 0:
            offers.append(Offer(pays=pays, gets=gets))
    offers.sort(key=lambda o: o.price)
    return tuple(offers)


SLOT_EXPIRED = 20  # auction_slot.time_interval once the 24 h slot has run out


def pool_fee(amm: dict, account: str | None) -> Decimal:
    """Trading fee as a fraction. The auction-slot holder and its authorized accounts pay the slot's
    discounted fee (a tenth of the usual one) while the slot is live."""
    units = amm["trading_fee"]
    slot = amm.get("auction_slot") or {}
    members = {slot.get("account")} | {x["account"] for x in slot.get("auth_accounts") or []}
    if account and account in members and slot.get("time_interval", SLOT_EXPIRED) < SLOT_EXPIRED:
        units = slot["discounted_fee"]
    return Decimal(units) / FEE_UNITS


def parse_pool(result: dict, a: Asset, b: Asset, account: str | None = None) -> Pool | None:
    amm = result["amm"]
    a1, a2 = amount_asset(amm["amount"]), amount_asset(amm["amount2"])
    if {a1, a2} != {a, b}:
        raise XrplError("badPool", f"amm_info returned {a1}/{a2}, asked for {a}/{b}")
    r1, r2 = amount_value(amm["amount"]), amount_value(amm["amount2"])
    if r1 <= 0 or r2 <= 0:
        return None
    return Pool(a1, a2, r1, r2, pool_fee(amm, account), amm.get("account"))


@dataclass
class Reads:
    pools: dict[frozenset, Pool | None]
    books: dict[tuple[Asset, Asset], tuple[Offer, ...]]
    skipped: list[str]  # reads whose response couldn't be understood; the pair is left out
    consistent: bool  # False if a read had to fall back to a newer ledger


def read_pairs(client: XrplClient, pairs: list[tuple[Asset, Asset]], ledger, book_limit: int = 60,
               workers: int = 6, account: str | None = None) -> Reads:
    """The AMM pool and both order books of each pair, read at `ledger` (an index or "validated") in parallel."""
    fell_back: list[bool] = []
    skipped: list[str] = []
    odd = (KeyError, TypeError, ValueError, ArithmeticError)  # a response that doesn't look like a pool or a book

    def pinned(read):
        try:
            return read(ledger)
        except XrplError as e:
            if e.code != "lgrNotFound":  # the node we landed on doesn't have that ledger (yet)
                raise
            fell_back.append(True)
            return read("validated")

    def read_pool(pair):
        a, b = pair
        try:
            return parse_pool(pinned(lambda at: client.amm_info(a, b, ledger=at)), a, b, account)
        except XrplError as e:
            if e.code == "badPool":
                skipped.append(f"pool {a.short()}/{b.short()}: {e}")
            elif e.code != "actNotFound":
                raise
        except odd as e:
            skipped.append(f"pool {a.short()}/{b.short()}: {type(e).__name__} {e}")
        return None

    def read_book(edge):
        x, y = edge  # offers selling y for x
        try:
            return parse_offers(pinned(lambda at: client.book_offers(taker_gets=y, taker_pays=x, limit=book_limit, ledger=at)))
        except odd as e:
            skipped.append(f"book {x.short()}->{y.short()}: {type(e).__name__} {e}")
            return ()

    edges = [e for a, b in pairs for e in ((a, b), (b, a))]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        pools = dict(zip((frozenset(p) for p in pairs), ex.map(read_pool, pairs)))
        books = dict(zip(edges, ex.map(read_book, edges)))
    return Reads(pools, books, sorted(skipped), not fell_back)


def build_markets(edges, pools: dict, books: dict, rates: RateCache) -> dict[tuple[Asset, Asset], Market]:
    return {(x, y): Market(x, y, books[(x, y)], pools[frozenset((x, y))], rates.get(x)) for x, y in edges}


def take_snapshot(client: XrplClient, assets: list[Asset], rates: RateCache, book_limit: int = 60,
                  pairs: list[tuple[Asset, Asset]] | None = None, workers: int = 6,
                  account: str | None = None) -> Snapshot:
    """Order books (both directions) and the AMM pool of each pair, all read from one validated ledger.

    `pairs` limits which pairs are read; by default every pair among XRP and `assets`, which costs
    n*(n-1) book requests plus n*(n-1)/2 pool requests for n = len(assets) + 1. `account` is the
    wallet that would trade: it gets the discounted fee in pools whose live auction slot it belongs to.
    """
    if pairs is None:
        pairs = list(combinations([XRP] + [a for a in assets if not a.is_xrp], 2))
    else:
        pairs = list({frozenset(p): p for p in pairs}.values())  # (a, b) and (b, a) are one pair
    pin = client.validated_ledger()
    reads = read_pairs(client, pairs, pin, book_limit, workers, account)
    edges = [e for a, b in pairs for e in ((a, b), (b, a))]
    return Snapshot(build_markets(edges, reads.pools, reads.books, rates), pin, reads.consistent,
                    sum(bool(o) for o in reads.books.values()), sum(p is not None for p in reads.pools.values()),
                    reads.skipped)
