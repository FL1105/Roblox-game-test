"""Everything the dashboard shows, behind one lock. `version` bumps on every change so the stream can wait for news."""

from __future__ import annotations

import threading
import time
from collections import deque


class BotState:
    def __init__(self, cfg, mode: str, source: str, node: str = ""):
        self._cond = threading.Condition()
        self.version = 0
        self.cfg = cfg
        self.mode, self.source, self.node = mode, source, node  # mode: "paper" | "live"; source: "xrpl" | "demo"
        self.started = time.time()
        self.running = True  # the Start/Stop switch: when off the bot still watches but opens nothing
        self.halted: str | None = None  # set when the bot stops itself; needs a person to resume
        self.connected = False
        self.ledger = {"index": 0, "close_time": 0.0, "seen_at": 0.0, "fetch_ms": 0.0}
        self.names: dict[str, str] = {}  # asset key -> display name
        self.markets: list[dict] = []
        self.routes: list[dict] = []
        self.series: deque[dict] = deque(maxlen=900)
        self.trades: deque[dict] = deque(maxlen=200)
        self.events: deque[dict] = deque(maxlen=120)
        self.account: dict | None = None  # live mode only: balances, never keys
        self.stats = dict(ledgers=0, routes_seen=0, profitable=0, executable=0, opened=0, won=0, lost=0, missed=0, stuck=0,
                          pnl_theory=0.0, pnl_real=0.0, fees=0.0, best_edge_bps=None, best_edge_route="")

    def change(self, fn=None) -> None:
        with self._cond:
            if fn:
                fn()
            self.version += 1
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float = 15.0) -> int:
        with self._cond:
            if self.version == seen:
                self._cond.wait(timeout)
            return self.version

    def log(self, level: str, msg: str) -> None:
        self.change(lambda: self.events.appendleft({"t": time.time(), "level": level, "msg": msg}))

    def set_running(self, running: bool) -> None:
        def go():
            self.running = running
            if running:
                self.halted = None
            self.events.appendleft({"t": time.time(), "level": "info", "msg": "started" if running else "stopped (watching only)"})
        self.change(go)

    def halt(self, reason: str) -> None:
        def go():
            self.running, self.halted = False, reason
            self.events.appendleft({"t": time.time(), "level": "error", "msg": f"HALTED: {reason}"})
        self.change(go)

    def snapshot(self) -> dict:
        with self._cond:
            now = time.time()
            return {
                "version": self.version, "now": now, "mode": self.mode, "source": self.source, "node": self.node,
                "running": self.running, "halted": self.halted, "connected": self.connected,
                "uptime": now - self.started, "ledger": dict(self.ledger),
                "config": {"base": self.cfg.base, "max_hops": self.cfg.max_hops, "fee_drops": self.cfg.fee_drops, **self.cfg.tunables()},
                "markets": list(self.markets), "routes": list(self.routes), "series": list(self.series),
                "trades": list(self.trades), "events": list(self.events), "account": self.account, "stats": dict(self.stats),
            }
