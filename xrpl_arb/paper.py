"""Paper execution: the shared plan (see plan.py) filled against later ledgers instead of the network.

A route is opened at the ledger where it was seen. Its first leg lands one ledger later, the next leg one ledger after that, and so
on, because the real bot cannot send a leg until the previous one is validated (the ledger has no way to make a multi-leg cycle
atomic: XRP-to-XRP payments with SendMax are malformed, and a same-issuer cycle is rejected as a path loop). Each leg is priced from
that later ledger's real books and pools, so the result includes the cost of being late and of the market moving between legs."""

from __future__ import annotations

from dataclasses import dataclass

from .assets import Asset
from .config import Config
from .executor import Executor
from .feed import Snapshot
from .plan import Attempt, Result, run_plan
from .routes import Evaluation

XRP = Asset("XRP")
MAX_WAIT_LEDGERS = 3  # how long a leg waits for market data before it counts as failed


def fee_in_base(snap: Snapshot, base, cfg: Config, txs: int = 1) -> float:
    """Network fee for `txs` transactions, in base units (0 if there is no XRP/base market to price it)."""
    if base.is_xrp:
        return txs * cfg.fee_drops / 1e6
    hop = snap.hop(XRP, base)
    if hop is None:
        return 0.0
    return txs * cfg.fee_drops / 1e6 * hop.quote(1.0, snap.fresh_pools()).out


@dataclass
class PaperTrade:
    id: int
    ev: Evaluation  # the plan, as quoted when the route was seen
    opened_ledger: int
    gen: object = None
    attempt: Attempt | None = None
    last_ledger: int = 0
    txs: int = 0


class PaperExecutor(Executor):
    def __init__(self, base, state):
        super().__init__(base, state)
        self.open_trade: PaperTrade | None = None

    @property
    def busy(self) -> bool:
        return self.open_trade is not None

    def open(self, ev: Evaluation, snap: Snapshot) -> int:
        t = PaperTrade(self._begin(ev, snap.ledger), ev, snap.ledger, last_ledger=snap.ledger)
        t.gen = run_plan(ev, self.cfg)
        t.attempt = next(t.gen)
        self.open_trade = t
        return t.id

    def on_snapshot(self, snap: Snapshot) -> None:
        """Resolve the pending attempt against this (later) ledger, and any attempt that follows on the same ledger."""
        t = self.open_trade
        if t is None or snap.ledger <= t.last_ledger:
            return
        route = t.ev.route
        while True:
            a = t.attempt
            hop = snap.hop(route.assets[a.leg], route.assets[a.leg + 1])
            if hop is None:
                if snap.ledger <= t.last_ledger + MAX_WAIT_LEDGERS:
                    return  # try again on the next ledger
                res = Result(False, note="could not be priced: market data lost")
            else:
                q = hop.quote(a.spend, snap.fresh_pools())
                ok = q.out >= a.min_out and q.spent >= a.spend * (1 - 1e-9)
                planned = t.ev.legs[a.leg].amount_out
                res = Result(ok, q.out if ok else 0.0, sent=ok, note="" if ok else f"would fill {q.out / planned - 1:+.2%} vs quote")
            t.txs += res.sent
            try:
                t.attempt = t.gen.send(res)
            except StopIteration as stop:
                self.open_trade = None
                self._end(t.id, stop.value, fee_in_base(snap, self.base, self.cfg, t.txs))
                return
            if t.attempt.kind != "unwind":  # the next leg needs a later ledger; an unwind is tried at once
                t.last_ledger = snap.ledger
                return self._progress(t.id, f"leg {t.attempt.leg} filled at ledger {snap.ledger}; leg {t.attempt.leg + 1} of {len(t.ev.legs)} next")
