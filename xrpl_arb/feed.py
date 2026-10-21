"""Where market data comes from. A feed turns the world into `Snapshot`s: every watched book and pool as of ONE validated ledger."""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .assets import Asset, amount_value
from .config import Config
from .liquidity import Hop, Offer, Pool, pool_key
from .rpc import Rpc, RpcError

RIPPLE_EPOCH = 946_684_800
NO_AMM_RECHECK_S = 600.0
Log = Callable[[str, str], None]


@dataclass
class Snapshot:
    ledger: int
    close_time: float  # unix seconds
    fetched_at: float
    source: str  # "xrpl" or "demo"
    books: dict[tuple[str, str], list[Offer]] = field(default_factory=dict)  # (pay key, receive key) -> offers, best first
    pools: dict[str, Pool] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)  # issuer transfer rate per asset key (1.0 = free)
    failed: set[tuple[str, str]] = field(default_factory=set)  # directed pairs we could not read this ledger
    fetch_s: float = 0.0  # how long reading every book and pool took

    def hop(self, src: Asset, dst: Asset) -> Hop | None:
        if (src.key, dst.key) in self.failed or (dst.key, src.key) in self.failed:
            return None
        offers = self.books.get((src.key, dst.key))
        pool = self.pools.get(pool_key(src, dst))
        if offers is None and pool is None:
            return None
        return Hop(src, dst, offers or [], pool, self.rates.get(src.key, 1.0), self.rates.get(dst.key, 1.0))

    def fresh_pools(self) -> dict[str, list[float]]:
        return {k: [p.ra, p.rb] for k, p in self.pools.items()}


def parse_offers(rows: list[dict], skip_owner: str = "") -> list[Offer]:
    """book_offers rows (taker pays TakerPays, receives TakerGets) -> Offers, using the funded amounts when the owner is short."""
    out = []
    for r in rows:
        if r.get("Account") == skip_owner:
            continue
        gets = amount_value(r.get("taker_gets_funded", r["TakerGets"]))
        pays = amount_value(r.get("taker_pays_funded", r["TakerPays"]))
        if gets > 0 and pays > 0:
            out.append(Offer(pays, gets, r.get("Account", "")))
    out.sort(key=lambda o: o.price)
    return out


def parse_pool(info: dict, a: Asset, b: Asset) -> Pool | None:
    amm = info.get("amm")
    if not amm or amm.get("asset_frozen") or amm.get("asset2_frozen"):
        return None
    first, second = amm["amount"], amm["amount2"]
    asset_first = Asset.from_amount(first)
    if asset_first == b:  # reply order follows the request, but do not rely on it
        first, second = second, first
    ra, rb = amount_value(first), amount_value(second)
    if ra <= 0 or rb <= 0:
        return None
    return Pool(a, b, ra, rb, amm.get("trading_fee", 0) / 100_000)


class LiveFeed:
    """Real XRPL mainnet data through public JSON-RPC servers. Read-only."""

    source = "xrpl"

    def __init__(self, cfg: Config, rpc: Rpc, log: Log = lambda level, msg: None, skip_owner: str = ""):
        self.cfg, self.rpc, self.log, self.skip_owner = cfg, rpc, log, skip_owner
        self.assets = cfg.assets()
        self.pairs = [(self.assets[a], self.assets[b]) for a, b in cfg.markets]
        self._rates: dict[str, float] = {}
        self._rates_at = 0.0
        self._warned: dict[str, float] = {}
        self._no_amm: dict[str, float] = {}  # pair key -> until when we trust that it has no pool
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="xrpl-feed")

    def latest(self) -> tuple[int, float]:
        r = self.rpc.call("ledger", ledger_index="validated")
        led = r["ledger"]
        return int(led["ledger_index"]), float(led["close_time"]) + RIPPLE_EPOCH

    def ledger_time(self, index: int) -> float:
        """Close time (unix seconds) of a past or present validated ledger."""
        return float(self.rpc.call("ledger", ledger_index=index)["ledger"]["close_time"]) + RIPPLE_EPOCH

    def _transfer_rates(self) -> dict[str, float]:
        if time.time() - self._rates_at < 600 and self._rates:
            return self._rates
        keys: dict[str, list[str]] = {}
        for a in self.assets.values():
            if not a.is_xrp:
                keys.setdefault(a.issuer, []).append(a.key)
        rates = dict(self._rates)
        for issuer, ks in keys.items():
            try:
                data = self.rpc.call("account_info", account=issuer, ledger_index="validated")["account_data"]
                rate = int(data.get("TransferRate", 1_000_000_000)) / 1_000_000_000
            except (RpcError, OSError, KeyError, ValueError) as e:
                self.log("warn", f"transfer rate for {issuer} unavailable ({e}); assuming none" if issuer not in rates else f"transfer rate refresh failed: {e}")
                rate = rates.get(ks[0], 1.0)
            for k in ks:
                rates[k] = rate
        self._rates, self._rates_at = rates, time.time()
        return rates

    def snapshot(self, ledger: int, close_time: float) -> Snapshot:
        snap = Snapshot(ledger, close_time, time.time(), self.source, rates=dict(self._transfer_rates()))
        depth = self.cfg.book_depth

        def book(src: Asset, dst: Asset):
            r = self.rpc.call("book_offers", taker_gets=dst.to_json(), taker_pays=src.to_json(), limit=depth, ledger_index=ledger)
            return (src.key, dst.key), parse_offers(r.get("offers", []), self.skip_owner)

        def amm(a: Asset, b: Asset):
            key = pool_key(a, b)
            if self._no_amm.get(key, 0.0) > time.time():
                return key, None
            try:
                return key, parse_pool(self.rpc.call("amm_info", asset=a.to_json(), asset2=b.to_json(), ledger_index=ledger), a, b)
            except RpcError as e:
                if e.code == "actNotFound":
                    self._no_amm[key] = time.time() + NO_AMM_RECHECK_S  # pools are rare to appear; do not ask every ledger
                    return key, None
                raise

        jobs = []
        for a, b in self.pairs:
            jobs += [("book", (a, b), self._pool.submit(book, a, b)), ("book", (b, a), self._pool.submit(book, b, a)), ("amm", (a, b), self._pool.submit(amm, a, b))]
        for kind, (a, b), fut in jobs:
            try:
                key, val = fut.result()
            except (RpcError, OSError) as e:
                snap.failed.add((a.key, b.key))
                tag = f"{kind} {a.code}/{b.code}"
                if time.time() - self._warned.get(tag, 0.0) > 60:  # say so, but not every ledger
                    self._warned[tag] = time.time()
                    self.log("warn", f"{tag} could not be read: {e}")
                continue
            if kind == "book":
                snap.books[key] = val
            elif val is not None:
                snap.pools[key] = val
        snap.fetch_s = time.time() - snap.fetched_at
        return snap

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class DemoFeed:
    """Synthetic markets, for trying the dashboard with no network. Prices random-walk; the AMM pools follow the true price lazily
    and the order-book makers lag it, so dislocations appear now and then. Nothing here is real."""

    source = "demo"

    def __init__(self, cfg: Config, seed: int | None = None, interval: float = 2.5):
        self.cfg, self.rng, self.interval = cfg, random.Random(seed), interval
        self.assets = cfg.assets()
        self.pairs = [(self.assets[a], self.assets[b]) for a, b in cfg.markets]
        self.ledger, self.t = 100_000_000, time.time()
        self.usd = {a.key: 1.0 + self.rng.uniform(-0.002, 0.002) for a in self.assets.values()}  # USD value of one unit, by asset key
        self.usd["XRP"] = 1.33
        self.pool_px = {i: self.usd[a.key] / self.usd[b.key] for i, (a, b) in enumerate(self.pairs)}  # b per a
        self.maker_px = dict(self.pool_px)

    def latest(self) -> tuple[int, float]:
        if time.time() - self.t < self.interval:  # a new "ledger" every few seconds, like the real thing
            return self.ledger, self.t
        return self.ledger + 1, time.time()

    def snapshot(self, ledger: int, close_time: float) -> Snapshot:
        self.ledger, self.t = ledger, close_time
        rng = self.rng
        for k in self.usd:
            self.usd[k] *= 1 + rng.gauss(0, 0.0006 if k == "XRP" else 0.0001)
        if rng.random() < 0.15:  # a sharp move in XRP
            self.usd["XRP"] *= 1 + rng.choice((-1, 1)) * rng.uniform(0.003, 0.012)
        snap = Snapshot(ledger, close_time, time.time(), self.source)
        for i, (a, b) in enumerate(self.pairs):
            true = self.usd[a.key] / self.usd[b.key]
            self.pool_px[i] += (true - self.pool_px[i]) * rng.uniform(0.3, 1.0)
            self.maker_px[i] += (true - self.maker_px[i]) * rng.uniform(0.05, 0.5)
            depth_b = 2_000_000 if a.is_xrp else 400_000
            ra = depth_b / self.pool_px[i]
            snap.pools[pool_key(a, b)] = Pool(a, b, ra, depth_b, 0.002)
            mid, half = self.maker_px[i], 0.0004  # mid = b per a
            sizes = (300, 800, 2000, 5000)
            snap.books[(b.key, a.key)] = [Offer(s * mid * (1 + half + 0.0003 * n), s, "demo") for n, s in enumerate(sizes)]  # pay b, get s of a
            snap.books[(a.key, b.key)] = [Offer(s / (mid * (1 - half - 0.0003 * n)), s, "demo") for n, s in enumerate(sizes)]  # pay a, get s of b
        return snap
