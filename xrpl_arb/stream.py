"""Watching the ledger as it closes instead of polling it.

A websocket subscription to `ledger` and `transactions` says when a ledger closed and what every transaction in it
touched. Only the pairs whose order books or AMM pool changed are read again (pinned to that ledger), the cycles are
re-evaluated, and each opportunity is followed until it disappears, so its lifetime is measured in ledgers.

Transactions are not delivered in tidy per-ledger bursts (those of an older ledger can arrive after a newer ledger's
close), so the stream only decides *which pairs to re-read*; the state itself is always read from the ledger. A late
message just causes one redundant read. Every `full_every` ledgers all pairs are re-read as a safety net.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal

from .client import XrplClient
from .cycles import Opportunity, candidates, passes
from .model import Asset
from .snapshot import RateCache, build_markets, read_pairs

DEFAULT_WS = ("wss://xrplcluster.com/", "wss://s1.ripple.com/")


def raw_key(amount) -> tuple[str, str | None]:
    """(currency, issuer) of a ledger amount without building an Asset (this runs for every transaction)."""
    if isinstance(amount, str):
        return ("XRP", None)
    cur = amount["currency"]
    return (cur.upper() if len(cur) == 40 else cur, amount["issuer"])


class PairIndex:
    """Maps what a transaction touched back to the configured pairs."""

    def __init__(self, pairs: list[tuple[Asset, Asset]]):
        self.pairs = list({frozenset(p): p for p in pairs}.values())
        self.by_offer = {frozenset(((a.currency, a.issuer), (b.currency, b.issuer))): frozenset((a, b))
                         for a, b in self.pairs}
        self.by_amm_account: dict[str, frozenset] = {}

    def learn_accounts(self, pools: dict) -> None:
        for key, pool in pools.items():
            if pool is not None and pool.account:
                self.by_amm_account[pool.account] = key

    def touched(self, msg: dict) -> set[frozenset]:
        """Pairs whose books or pool a validated transaction changed, from its metadata: an Offer node names its
        two assets; a swap moves the AMM account's XRP (AccountRoot) or its issued balance (RippleState)."""
        hit: set[frozenset] = set()
        for node in (msg.get("meta") or {}).get("AffectedNodes", ()):
            body = node.get("ModifiedNode") or node.get("CreatedNode") or node.get("DeletedNode")
            if not body:
                continue
            kind = body.get("LedgerEntryType")
            fields = body.get("FinalFields") or body.get("NewFields") or {}
            if kind == "Offer":
                gets, pays = fields.get("TakerGets"), fields.get("TakerPays")
                if gets is not None and pays is not None:
                    pair = self.by_offer.get(frozenset((raw_key(gets), raw_key(pays))))
                    if pair:
                        hit.add(pair)
            elif kind == "AccountRoot":
                pair = self.by_amm_account.get(fields.get("Account"))
                if pair:
                    hit.add(pair)
            elif kind == "RippleState":
                for side in ("HighLimit", "LowLimit"):
                    pair = self.by_amm_account.get((fields.get(side) or {}).get("issuer"))
                    if pair:
                        hit.add(pair)
        return hit


class BookState:
    """Order books and pools for every configured pair, re-read selectively."""

    def __init__(self, client: XrplClient, index: PairIndex, rates: RateCache, book_limit: int = 60,
                 workers: int = 6, account: str | None = None):
        self.client, self.index, self.rates = client, index, rates
        self.book_limit, self.workers, self.account = book_limit, workers, account
        self.pools: dict = {}
        self.books: dict = {}
        self.markets: dict = {}

    def refresh(self, dirty: set[frozenset] | None, ledger):
        """Re-read the dirty pairs (all of them if `dirty` is None) at `ledger`; returns the Reads."""
        pairs = self.index.pairs if dirty is None else [p for p in self.index.pairs if frozenset(p) in dirty]
        reads = read_pairs(self.client, pairs, ledger, self.book_limit, self.workers, self.account)
        self.pools.update(reads.pools)
        self.books.update(reads.books)
        self.index.learn_accounts(reads.pools)
        edges = [e for a, b in pairs for e in ((a, b), (b, a))]
        self.markets.update(build_markets(edges, self.pools, self.books, self.rates))
        return reads, len(pairs)


@dataclass
class Lifetimes:
    """Follows each opportunity (by path) from the ledger it first appears until it disappears."""

    live: dict = field(default_factory=dict)

    def update(self, ledger: int, opps: list[Opportunity]) -> tuple[list[Opportunity], list[dict]]:
        """(opportunities seen for the first time, records of those that just ended)."""
        now = {tuple(o.path): o for o in opps}
        new = [o for k, o in now.items() if k not in self.live]
        for k, o in now.items():
            rec = self.live.setdefault(k, {"first": ledger, "best": o.profit, "best_in": o.amount_in})
            rec["last"] = ledger
            if o.profit > rec["best"]:
                rec["best"], rec["best_in"] = o.profit, o.amount_in
        ended = []
        for k in [k for k in self.live if k not in now]:
            rec = self.live.pop(k)
            ended.append({"path": list(k), "first_ledger": rec["first"], "last_ledger": rec["last"],
                          "ledgers_alive": rec["last"] - rec["first"] + 1, "best_profit": str(rec["best"]),
                          "best_amount_in": str(rec["best_in"])})
        return new, ended


def _default_connect(url: str):
    from websockets.sync.client import connect
    return connect(url, open_timeout=20, max_size=None)


def watch(client: XrplClient, pairs: list[tuple[Asset, Asset]], settings, *, connect=_default_connect,
          clock=time.monotonic, wall=time.time, log=print, sink=None) -> int:
    """Run until `settings.minutes` / `settings.max_ledgers` is reached (0 = forever) or interrupted."""
    try:
        from websockets.exceptions import WebSocketException
    except ImportError:  # only the injected test connection is usable without the library
        WebSocketException = OSError
    index = PairIndex(pairs)
    rates = RateCache(client)
    for a, b in index.pairs:  # transfer rates once, before the stream starts, so the first ledger isn't slow
        rates.get(a), rates.get(b)
    state = BookState(client, index, rates, settings.book_limit, settings.workers, settings.account)
    tracker = Lifetimes()
    min_profit, min_pct = Decimal(str(settings.min_profit)), Decimal(str(settings.min_profit_pct))
    max_xrp, tx_cost = Decimal(str(settings.max_xrp)), Decimal(str(settings.tx_cost))
    started, processed, since_full, attempt = clock(), 0, 0, 0
    urls = list(settings.ws)

    def write(rows):
        if sink:
            sink.write("".join(json.dumps(r) + "\n" for r in rows))
            sink.flush()

    def done() -> bool:
        return bool((settings.max_ledgers and processed >= settings.max_ledgers)
                    or (settings.minutes and clock() - started >= settings.minutes * 60))

    def process(ledger: int, closed_at: float, dirty: set, full: bool) -> None:
        nonlocal processed, since_full
        began = clock()
        reads, n_pairs = state.refresh(None if full else dirty, ledger)
        read_done = clock()
        cands = candidates(state.markets, max_xrp=max_xrp, tx_cost=tx_cost, max_legs=settings.max_legs)
        opps = [o for o in cands if passes(o, min_profit, min_pct)]
        new, ended = tracker.update(ledger, opps)
        eval_done = clock()
        since_full = 0 if full else since_full + 1
        processed += 1
        total = eval_done - closed_at
        log(f"ledger {ledger}: {'ALL' if full else n_pairs} pairs read in {read_done - began:.2f}s, "
            f"{len(cands)} candidates, {len(opps)} opportunities ({len(new)} new, {len(ended)} ended), "
            f"{total:.2f}s after close"
            + (f", {len(reads.skipped)} unreadable" if reads.skipped else "") + ("" if reads.consistent else ", fell back"))
        for o in new:
            log("  NEW " + _describe(o))
        for e in ended:
            log(f"  ended after {e['ledgers_alive']} ledger(s), best {Decimal(e['best_profit']):+.6f} XRP: "
                f"{' -> '.join(Asset.parse(p).short() for p in e['path'])}")
        t = round(wall(), 1)
        write([{"type": "ledger", "t": t, "ledger": ledger, "pairs_read": n_pairs, "full": full,
                "read_s": round(read_done - began, 3), "eval_s": round(eval_done - read_done, 3),
                "after_close_s": round(total, 3),
                "candidates": len(cands), "opportunities": len(opps), "skipped": len(reads.skipped),
                "consistent": reads.consistent,
                "best_candidate": cands[0].to_json() if cands and not opps else None}]
              + [{"type": "opp", "t": t, "ledger": ledger, **o.to_json()} for o in new]
              + [{"type": "end", "t": t, **e} for e in ended])

    try:
        while not done():
            url = urls[attempt % len(urls)]
            try:
                with connect(url) as ws:
                    ws.send(json.dumps({"command": "subscribe", "streams": ["ledger", "transactions"]}))
                    log(f"connected to {url}")
                    attempt = 0
                    first, dirty, pending = True, set(), None  # pending: (ledger, closed_at, deadline)
                    while not done():
                        wait = 30.0 if pending is None else max(0.01, pending[2] - clock())
                        try:
                            raw = ws.recv(timeout=wait)
                        except TimeoutError:
                            if pending is None:
                                raise ConnectionError("stream silent for 30 s")
                            raw = None
                        if raw is not None:
                            msg = json.loads(raw)
                            kind = msg.get("type")
                            if kind == "ledgerClosed":
                                pending = (msg["ledger_index"], clock(), clock() + settings.settle)
                            elif kind == "transaction" and msg.get("validated"):
                                dirty |= index.touched(msg)
                        if pending is not None and clock() >= pending[2]:
                            full = first or since_full >= settings.full_every
                            process(pending[0], pending[1], dirty, full)
                            first, dirty, pending = False, set(), None
                return 0
            except (OSError, ConnectionError, WebSocketException, json.JSONDecodeError) as e:
                attempt += 1
                log(f"stream error ({type(e).__name__}: {e}); reconnecting")
                time.sleep(min(2 ** attempt, 20))
    except KeyboardInterrupt:
        return 0
    return 0


def _describe(opp: Opportunity) -> str:
    path = " -> ".join(Asset.parse(p).short() for p in opp.path)
    return f"{opp.profit:+.6f} XRP ({opp.profit_pct:+.3f}%) in {opp.amount_in:f} out {opp.amount_out:f}  {path}"
