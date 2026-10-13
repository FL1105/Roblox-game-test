"""Thread-safe state behind the terminal UI: one paper engine plus a log, a token table and settings."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import replace

from ..paper.engine import ClosedTrade, PaperEngine, Strategy
from ..pump.curve import quote_sell
from ..pump.events import TOTAL_SUPPLY, Event, NewToken, ScanNote, Trade

DAY_MS = 86_400_000
MAX_ROWS = 80
ALLOWED_VERDICTS = ("PASS", "CAUTION", "AVOID")
VIRTUAL_START_SOL = 30.0  # a fresh curve starts with 30 virtual SOL; anything above is SOL actually raised


def default_strategy() -> Strategy:
    return Strategy(size_sol=0.1, size_caution_sol=0.05)


def settings_dict(s: Strategy) -> dict:
    return {
        "amount_min": s.size_caution_sol if s.size_caution_sol is not None else s.size_sol,
        "amount_max": s.size_sol,
        "take_profit": s.take_profit_pct,
        "stop_loss": s.stop_loss_pct,
        "max_hold_s": s.max_hold_s,
        "max_open": s.max_open,
        "latency_ms": s.latency_ms,
        "fee_bps": s.fee_bps,
        "tx_cost_sol": s.tx_cost_sol,
        "max_dev_buy_pct": s.max_dev_buy_pct,
        "max_entry_mcap_sol": s.max_entry_mcap_sol,
        "verdicts": sorted(s.allowed_verdicts),
        "exit_on_creator_sell": s.exit_on_creator_sell,
    }


def _num(data: dict, key: str, label: str, lo: float, hi: float, current: float) -> float:
    if key not in data:
        return current
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{label} must be a number between {lo:g} and {hi:g}")
    return float(v)


def parse_settings(data: dict, current: Strategy) -> Strategy:
    """Validate a (possibly partial) settings payload from the UI and return the new Strategy."""
    cur = settings_dict(current)
    unknown = set(data) - set(cur)
    if unknown:
        raise ValueError(f"unknown setting: {sorted(unknown)[0]}")

    lo = _num(data, "amount_min", "Minimum amount", 0.001, 100, cur["amount_min"])
    hi = _num(data, "amount_max", "Maximum amount", 0.001, 100, cur["amount_max"])
    if lo > hi:
        raise ValueError("Minimum amount can't be larger than the maximum")

    verdicts = data.get("verdicts", cur["verdicts"])
    if not isinstance(verdicts, list) or not verdicts or any(v not in ALLOWED_VERDICTS for v in verdicts):
        raise ValueError(f"verdicts must be a non-empty list drawn from {list(ALLOWED_VERDICTS)}")
    exit_cs = data.get("exit_on_creator_sell", cur["exit_on_creator_sell"])
    if not isinstance(exit_cs, bool):
        raise ValueError("exit_on_creator_sell must be true or false")

    return replace(
        current,
        size_sol=hi,
        size_caution_sol=lo,
        take_profit_pct=_num(data, "take_profit", "Take profit", 1, 1000, cur["take_profit"]),
        stop_loss_pct=_num(data, "stop_loss", "Stop loss", 1, 99, cur["stop_loss"]),
        max_hold_s=_num(data, "max_hold_s", "Max hold", 10, 3600, cur["max_hold_s"]),
        max_open=int(_num(data, "max_open", "Max open positions", 1, 20, cur["max_open"])),
        latency_ms=int(_num(data, "latency_ms", "Latency", 0, 5000, cur["latency_ms"])),
        fee_bps=_num(data, "fee_bps", "Fee", 0, 1000, cur["fee_bps"]),
        tx_cost_sol=_num(data, "tx_cost_sol", "Transaction cost", 0, 0.1, cur["tx_cost_sol"]),
        max_dev_buy_pct=_num(data, "max_dev_buy_pct", "Max dev buy", 0, 100, cur["max_dev_buy_pct"]),
        max_entry_mcap_sol=_num(data, "max_entry_mcap_sol", "Max entry market cap", 1, 1000, cur["max_entry_mcap_sol"]),
        allowed_verdicts=frozenset(verdicts),
        exit_on_creator_sell=exit_cs,
    )


class TerminalState:
    def __init__(self, strategy: Strategy | None = None, *, source: str = "demo", simulated: bool = True, start_sol: float = 1.0, log_size: int = 500):
        self.lock = threading.RLock()
        self.source, self.simulated, self.start_sol = source, simulated, start_sol
        self.strategy = strategy or default_strategy()
        self.running = False
        self.feed_status = "starting"  # starting | live | finished | error: <msg>
        self._log_size = log_size
        self._reset_locked()

    # ---- control ----
    def _reset_locked(self) -> None:
        self.logs: deque[dict] = deque(maxlen=self._log_size)
        self.log_total = 0
        self.tokens: OrderedDict[str, dict] = OrderedDict()
        self.notes: dict[str, str] = {}  # mint -> latest skip reason
        self.closed_by_mint: dict[str, ClosedTrade] = {}
        self.events_seen = 0
        self.clock = 0
        self.engine = PaperEngine(self.strategy, self.start_sol, on_note=self._on_note)
        self.engine.enabled = self.running

    def reset(self) -> None:
        with self.lock:
            self._reset_locked()
            self._log(int(time.time() * 1000), "info", "", "paper account reset")

    def set_running(self, running: bool) -> None:
        with self.lock:
            self.running = running
            self.engine.enabled = running
            self._log(self.clock or int(time.time() * 1000), "info", "", "bot started" if running else "bot stopped: no new positions will open")

    def update_settings(self, data: dict) -> dict:
        with self.lock:
            self.strategy = parse_settings(data, self.strategy)
            self.engine.s = self.strategy
            return settings_dict(self.strategy)

    def set_feed_status(self, status: str) -> None:
        with self.lock:
            self.feed_status = status
            if status.startswith("error") or status == "finished":
                self._log(self.clock or int(time.time() * 1000), "info", "", f"feed {status}")

    # ---- events ----
    def emit(self, ev: Event) -> None:
        with self.lock:
            self.events_seen += 1
            self.clock = max(self.clock, ev.t)
            if self.feed_status == "starting":
                self.feed_status = "live"
            if isinstance(ev, NewToken):
                self.tokens[ev.mint] = {"symbol": ev.symbol, "name": ev.name, "t": ev.t, "creator": ev.creator}
                while len(self.tokens) > 400:
                    old, _ = self.tokens.popitem(last=False)
                    self.notes.pop(old, None)
                    self.closed_by_mint.pop(old, None)
                self._log(ev.t, "launch", ev.mint, f"{ev.symbol} launched, dev bought {ev.dev_tokens / TOTAL_SUPPLY * 100:.1f}% of supply")
            elif isinstance(ev, ScanNote) and ev.mint in self.tokens:
                self._log(ev.t, "scan", ev.mint, f"{self.tokens[ev.mint]['symbol']} scanned: {ev.verdict}" + (f" ({ev.summary})" if ev.summary else ""))
            self.engine.on_event(ev)

    def _on_note(self, t: int, kind: str, mint: str, text: str) -> None:
        sym = self.tokens.get(mint, {}).get("symbol", mint[:6])
        if kind == "skip":
            self.notes[mint] = text
        elif kind == "exit":
            self.closed_by_mint[mint] = self.engine.closed[-1]
        self._log(t, kind, mint, f"{sym}: {text}")

    def _log(self, t: int, kind: str, mint: str, text: str) -> None:
        self.log_total += 1
        self.logs.appendleft({"t": t, "kind": kind, "mint": mint, "text": text})

    # ---- views ----
    def snapshot(self, window: str = "24h") -> dict:
        with self.lock:
            e = self.engine
            closed = e.closed if window == "all" else [c for c in e.closed if c.exit_t >= e.last_t - DAY_MS]
            wins = [c for c in closed if c.ret_pct > 0]
            equity = e.cash + sum(p.sol_in for p in e.positions.values())
            return {
                "running": self.running,
                "source": self.source,
                "simulated": self.simulated,
                "feed_status": self.feed_status,
                "clock": self.clock,
                "events_seen": self.events_seen,
                "paper": {"start": self.start_sol, "equity": equity, "cash": e.cash, "open": len(e.positions)},
                "settings": settings_dict(self.strategy),
                "stats": {
                    "window": window,
                    "trades": len(closed),
                    "wins": len(wins),
                    "win_rate": len(wins) / len(closed) * 100 if closed else 0.0,
                    "pnl_sol": sum(c.pnl_sol for c in closed),
                },
                "counters": {"launches": len(e.launch), "scanned": len(e.scans), "skipped": dict(e.skipped)},
                "rows": [self._row(m, t) for m, t in list(self.tokens.items())[-MAX_ROWS:][::-1]],
                "logs": list(self.logs)[:200],
                "log_total": self.log_total,
                "equity_curve": e.equity_curve[-300:],
            }

    def _row(self, mint: str, tok: dict) -> dict:
        e = self.engine
        curve = e.curve.get(mint)
        row = {
            "mint": mint, "symbol": tok["symbol"], "name": tok["name"], "t": tok["t"],
            "liquidity_sol": max(curve.v_sol - VIRTUAL_START_SOL, 0.0) if curve else None,
            "amount": None, "pnl_sol": None, "pnl_pct": None,
        }
        pos, closed = e.positions.get(mint), self.closed_by_mint.get(mint)
        scan = e.scans.get(mint)
        if pos is not None:
            net = quote_sell(curve, pos.tokens, e.s.fee_bps) - 2 * e.s.tx_cost_sol - pos.sol_in
            row.update(status="exiting" if pos.exit_ready_ms is not None else "holding", detail=pos.exit_reason or "",
                       amount=pos.sol_in, pnl_sol=net, pnl_pct=net / pos.sol_in * 100)
        elif closed is not None:
            row.update(status="closed", detail=closed.reason, amount=closed.sol_in, pnl_sol=closed.pnl_sol, pnl_pct=closed.ret_pct)
        elif mint in e.pending:
            row.update(status="entering", detail="")
        elif mint in self.notes:
            row.update(status="skipped", detail=self.notes[mint].removeprefix("skipped (").removesuffix(")"))
        elif scan is not None:
            row.update(status="watching", detail=scan.verdict)
        else:
            row.update(status="scanning", detail="")
        return row
