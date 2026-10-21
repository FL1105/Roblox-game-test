"""The loop: new ledger -> snapshot -> price every cycle -> maybe open one -> publish to the dashboard."""

from __future__ import annotations

import threading
import time
import traceback

from .config import KNOWN, Config
from .feed import Snapshot
from .liquidity import pool_key
from .paper import fee_in_base
from .plan import threshold
from .routes import Evaluation, Route, find_routes, optimize
from .state import BotState


class Bot:
    def __init__(self, cfg: Config, feed, state: BotState, executor, size_cap=None):
        self.cfg, self.feed, self.state, self.executor = cfg, feed, state, executor
        self.size_cap = size_cap  # live mode: callable -> most base we can spend right now
        assets = cfg.assets()
        self.base = assets[cfg.base]
        self.pairs = [(assets[a], assets[b]) for a, b in cfg.markets]
        self.routes = find_routes(self.base, self.pairs, cfg.max_hops)
        state.names = {a.key: n for n, a in KNOWN.items()}
        self._last_ledger = 0

    # -- one ledger --------------------------------------------------------------------------------------------------

    def step(self, snap: Snapshot) -> list[Evaluation]:
        cfg = self.state.cfg  # the dashboard may have changed it
        self.executor.on_snapshot(snap)
        fee = fee_in_base(snap, self.base, cfg)
        results: list[tuple[Route, Evaluation | str]] = []  # a string means "not evaluated", and says why
        for route in self.routes:
            hops = [snap.hop(route.assets[i], route.assets[i + 1]) for i in range(route.hops)]
            if any(h is None for h in hops):
                results.append((route, "a market on this route could not be read this ledger"))
                continue
            hi = cfg.size_max if self.size_cap is None else min(cfg.size_max, self.size_cap())
            if hi < cfg.size_min:
                results.append((route, "the wallet holds less than the smallest trade size"))
                continue
            results.append((route, optimize(route, hops, snap.fresh_pools, cfg.size_min, hi, fee_cost=fee * route.hops)))

        rows, best, executable = [], None, []
        for route, ev in results:
            label = route.label(lambda a: self.state.names.get(a.key, a.code))
            if isinstance(ev, str):
                rows.append({"id": route.id, "route": label, "hops": route.hops, "status": "NO DATA" if "market" in ev else "NOT TRADABLE", "note": ev})
                continue
            ok = ev.complete and ev.profit >= threshold(cfg, ev.size)
            cool = self.executor.on_cooldown(route.id, snap.ledger)
            blocked = self.executor.can_trade(route)
            status = ("NOT TRADABLE" if blocked else "COOLDOWN" if cool else "EXECUTABLE") if ok else ("BELOW THRESHOLD" if ev.profit > 0 and ev.complete else ("THIN BOOK" if not ev.complete else "NO EDGE"))
            rows.append({"id": route.id, "route": label, "hops": route.hops, "status": status, "size": ev.size, "out": ev.out, "profit": ev.profit,
                         "edge_bps": ev.edge_bps, "legs": [{"from": self.state.names.get(l.src.key, l.src.code), "to": self.state.names.get(l.dst.key, l.dst.code),
                                                           "in": l.amount_in, "out": l.amount_out, "amm": l.amm_share} for l in ev.legs]})
            if ev.complete and (best is None or ev.edge_bps > best[1].edge_bps):
                best = (label, ev)
            if ok and not cool and not blocked:
                executable.append(ev)
        rows.sort(key=lambda r: (r.get("edge_bps") is None, -(r.get("edge_bps") or 0)))

        opened = None
        st = self.state
        if executable and st.running and not self.executor.busy:
            opened = max(executable, key=lambda e: e.profit)
            self.executor.open(opened, snap)

        def publish():
            st.ledger.update(index=snap.ledger, close_time=snap.close_time, seen_at=time.time(), fetch_ms=snap.fetch_s * 1000)
            st.connected = True
            st.markets = self.market_rows(snap)
            st.routes = rows
            s = st.stats
            s["ledgers"] += 1
            s["routes_seen"] += sum(1 for r in rows if "edge_bps" in r)
            s["profitable"] += sum(1 for r in rows if r.get("profit", 0) > 0)
            s["executable"] += len(executable)
            if best:
                s["best_edge_bps"], s["best_edge_route"] = max(s["best_edge_bps"], best[1].edge_bps) if s["best_edge_bps"] is not None else best[1].edge_bps, best[0]
            st.series.append({"t": time.time(), "ledger": snap.ledger, "edge": best[1].edge_bps if best else None,
                              "theory": s["pnl_theory"], "real": s["pnl_real"], "threshold": cfg.min_profit_bps})
        st.change(publish)
        return executable

    def market_rows(self, snap: Snapshot) -> list[dict]:
        name = lambda a: self.state.names.get(a.key, a.code)
        out = []
        for a, b in self.pairs:
            pool = snap.pools.get(pool_key(a, b))
            asks = snap.books.get((b.key, a.key)) or []  # pay b, receive a
            bids = snap.books.get((a.key, b.key)) or []  # pay a, receive b
            ask = asks[0].price if asks else None  # b per a
            bid = 1 / bids[0].price if bids else None
            row = {"pair": f"{name(a)}/{name(b)}", "a": name(a), "b": name(b), "amm": None, "bid": bid, "ask": ask,
                   "bid_depth": sum(o.inp for o in bids[:20]), "ask_depth": sum(o.out for o in asks[:20]),
                   "venue_edge_bps": None, "spread_bps": ((ask / bid - 1) * 1e4) if ask and bid else None}
            if pool:
                mid = pool.rb / pool.ra
                row["amm"] = {"ra": pool.ra, "rb": pool.rb, "fee_bps": pool.fee * 1e4, "mid": mid}
                edges = []
                if bid:  # sell a to the book, buy it from the pool
                    edges.append(bid / (mid / (1 - pool.fee)) - 1)
                if ask:  # buy a from the book, sell it to the pool
                    edges.append(mid * (1 - pool.fee) / ask - 1)
                if edges:
                    row["venue_edge_bps"] = max(edges) * 1e4
            out.append(row)
        return out

    # -- loop --------------------------------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        st, backoff = self.state, 1.0
        while not stop.is_set():
            try:
                index, close = self.feed.latest()
                st.change(lambda: setattr(st, "connected", True))
                if index > self._last_ledger:
                    snap = self.feed.snapshot(index, close)
                    self._last_ledger = index
                    self.step(snap)
                backoff = 1.0
                stop.wait(self.state.cfg.poll_s)
            except (OSError, ValueError) as e:  # network trouble: say so, back off, keep going
                st.change(lambda: setattr(st, "connected", False))
                st.log("warn", f"data feed: {e}")
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as e:  # a bug must not freeze the dashboard silently: report it, keep the loop alive
                traceback.print_exc()
                st.log("error", f"internal error ({type(e).__name__}: {e}); see the console")
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
