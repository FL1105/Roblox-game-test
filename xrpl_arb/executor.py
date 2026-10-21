"""Bookkeeping common to the paper and live executors: trade rows, statistics, cooldowns, halting."""

from __future__ import annotations

import time

from .config import Config
from .plan import Outcome
from .routes import Evaluation, Route


class Executor:
    def __init__(self, base, state):
        self.base, self.state = base, state
        self._next_id = 1
        self.cooldown: dict[str, int] = {}

    @property
    def cfg(self) -> Config:  # read through the state so dashboard changes apply to trades already open
        return self.state.cfg

    @property
    def busy(self) -> bool:
        raise NotImplementedError

    def can_trade(self, route: Route) -> str | None:
        """None if this executor can trade the route, else the reason it cannot."""
        return None

    def on_cooldown(self, route_id: str, ledger: int) -> bool:
        return ledger < self.cooldown.get(route_id, 0)

    def name(self, asset) -> str:
        return self.state.names.get(asset.key, asset.code)

    def _begin(self, ev: Evaluation, ledger: int) -> int:
        tid = self._next_id
        self._next_id += 1
        self.cooldown[ev.route.id] = ledger + self.cfg.cooldown_ledgers
        row = {"id": tid, "t": time.time(), "ledger": ledger, "route": ev.route.label(self.name), "size": ev.size, "expected": ev.profit,
               "status": "OPEN", "realized": None, "note": "waiting for the next ledger"}
        st = self.state

        def go():
            st.trades.appendleft(row)
            st.stats["opened"] += 1
            st.stats["pnl_theory"] += ev.profit
        st.change(go)
        return tid

    def _progress(self, tid: int, note: str, status: str = "OPEN", realized: float | None = None) -> None:
        def go():
            for i, r in enumerate(self.state.trades):
                if r["id"] == tid:
                    self.state.trades[i] = {**r, "note": note, "status": status, "realized": realized}
                    break
        self.state.change(go)

    def _end(self, tid: int, out: Outcome, fee: float, ev: Evaluation | None = None) -> float:
        """Record a finished trade; returns its realized profit net of `fee`. A STUCK trade halts the bot."""
        realized = out.realized - fee
        key = {"WON": "won", "LOST": "lost", "MISSED": "missed", "STUCK": "stuck"}[out.status]
        st, unit = self.state, self.name(self.base)

        def go():
            st.stats[key] += 1
            st.stats["pnl_real"] += realized
            st.stats["fees"] += fee
            for i, r in enumerate(st.trades):
                if r["id"] == tid:
                    st.trades[i] = {**r, "note": out.note, "status": out.status, "realized": realized}
                    break
            st.events.appendleft({"t": time.time(), "level": "error" if out.status == "STUCK" else "info",
                                  "msg": f"trade #{tid} {out.status}: {out.note} ({realized:+.4f} {unit})"})
        st.change(go)
        if out.status == "STUCK":
            held = f"; the wallet is holding {out.holding[1]:.6g} {self.name(ev.route.assets[out.holding[0]])}" if out.holding and ev else ""
            st.halt(f"trade #{tid} could not be completed within the loss limit{held}")
        return realized
