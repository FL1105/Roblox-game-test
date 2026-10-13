"""Event-driven paper trader for pump.fun bonding-curve tokens.

Causality: a decision made at time T is filled at the curve state that was current at
T + latency (the last state seen *before* the first event at or after that time), so the
price can move against you while your transaction is in flight. Entry and exit both pay
the pump fee and a flat per-transaction cost (priority fee / tip).

What this does NOT model: other bots front-running you in the same slot, failed or
dropped transactions, your own price impact on others, and exits after graduation.
Treat results as an optimistic upper bound.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..pump.curve import CurveState, quote_buy, quote_sell
from ..pump.events import TOTAL_SUPPLY, Event, NewToken, ScanNote, Trade


@dataclass(frozen=True)
class Strategy:
    take_profit_pct: float = 30.0   # net of fees and tx costs
    stop_loss_pct: float = 20.0
    max_hold_s: float = 300.0
    size_sol: float = 0.1           # fixed size, unless size_frac is set
    size_frac: float | None = None  # fraction of equity per trade (compounding)
    max_open: int = 3
    latency_ms: int = 800
    fee_bps: float = 100            # pump.fun fee: verify the current rate
    tx_cost_sol: float = 0.003      # priority fee + tip, paid on each side
    exit_on_creator_sell: bool = True
    require_scan: bool = True
    allowed_verdicts: frozenset = frozenset({"PASS", "CAUTION"})
    max_dev_buy_pct: float = 15.0
    max_entry_mcap_sol: float = 60.0


@dataclass
class Position:
    mint: str
    creator: str
    entry_t: int
    sol_in: float
    tokens: float
    entry_mcap: float
    exit_reason: str | None = None
    exit_ready_ms: int | None = None


@dataclass(frozen=True)
class ClosedTrade:
    mint: str
    entry_t: int
    exit_t: int
    sol_in: float
    proceeds: float
    pnl_sol: float  # after both tx costs
    ret_pct: float
    reason: str
    entry_mcap: float
    exit_mcap: float


@dataclass
class EngineResult:
    strategy: Strategy
    start_sol: float
    final_equity: float
    closed: list[ClosedTrade]
    equity_curve: list[float]  # equity after each close, starting at start_sol
    skipped: Counter = field(default_factory=Counter)
    launches_seen: int = 0


class PaperEngine:
    def __init__(self, strategy: Strategy, start_sol: float = 1.0):
        self.s = strategy
        self.start_sol = start_sol
        self.cash = start_sol
        self.curve: dict[str, CurveState] = {}
        self.launch: dict[str, NewToken] = {}
        self.scans: dict[str, ScanNote] = {}
        self.pending: dict[str, int] = {}
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.equity_curve = [start_sol]
        self.skipped: Counter = Counter()
        self.last_t = 0

    # ---- public ----
    def on_event(self, ev: Event) -> None:
        self.last_t = max(self.last_t, ev.t)
        self._request_timeouts(ev.t)
        self._fill_due(ev.t)
        if isinstance(ev, NewToken):
            self.launch[ev.mint] = ev
            self.curve[ev.mint] = CurveState(ev.v_sol, ev.v_tokens)
            if not self.s.require_scan:
                self._consider_entry(ev.mint, ev.t)
        elif isinstance(ev, ScanNote):
            self.scans[ev.mint] = ev
            if self.s.require_scan and ev.mint in self.launch:
                if ev.verdict in self.s.allowed_verdicts:
                    self._consider_entry(ev.mint, ev.t)
                else:
                    self.skipped[f"verdict:{ev.verdict}"] += 1
        elif isinstance(ev, Trade):
            self._on_trade(ev)

    def finish(self) -> EngineResult:
        """Close whatever is still open at the last known price and return the result."""
        for pos in list(self.positions.values()):
            self._close(pos, self.last_t, pos.exit_reason or "end_of_data")
        if self.s.require_scan:
            self.skipped["no_scan"] += sum(1 for m in self.launch if m not in self.scans)
        self.skipped["entry_never_filled"] += len(self.pending)
        return EngineResult(
            self.s, self.start_sol, self.cash, self.closed, self.equity_curve, self.skipped, len(self.launch)
        )

    # ---- entries ----
    def _consider_entry(self, mint: str, decision_t: int) -> None:
        launch, state = self.launch[mint], self.curve[mint]
        if mint in self.pending or mint in self.positions:
            return
        if launch.dev_tokens / TOTAL_SUPPLY * 100 > self.s.max_dev_buy_pct:
            self.skipped["dev_buy"] += 1
        elif state.mcap_sol > self.s.max_entry_mcap_sol:
            self.skipped["mcap_too_high"] += 1
        elif len(self.positions) + len(self.pending) >= self.s.max_open:
            self.skipped["max_open"] += 1
        else:
            self.pending[mint] = decision_t + self.s.latency_ms

    def _open(self, mint: str, t: int) -> None:
        state = self.curve[mint]
        if state.mcap_sol > self.s.max_entry_mcap_sol:
            self.skipped["mcap_too_high_at_fill"] += 1
            return
        equity = self.cash + sum(p.sol_in for p in self.positions.values())
        size = equity * self.s.size_frac if self.s.size_frac is not None else self.s.size_sol
        if self.cash < size + self.s.tx_cost_sol or size <= 0:
            self.skipped["no_cash"] += 1
            return
        tokens = quote_buy(state, size, self.s.fee_bps)
        self.cash -= size + self.s.tx_cost_sol
        self.positions[mint] = Position(mint, self.launch[mint].creator, t, size, tokens, state.mcap_sol)

    # ---- exits ----
    def _on_trade(self, tr: Trade) -> None:
        state = CurveState(tr.v_sol, tr.v_tokens)
        self.curve[tr.mint] = state
        pos = self.positions.get(tr.mint)
        if pos is None or pos.exit_ready_ms is not None:
            return
        reason = None
        if self.s.exit_on_creator_sell and tr.trader == pos.creator and tr.side == "sell":
            reason = "creator_sold"
        elif state.graduated:
            reason = "graduated"
        else:
            net = quote_sell(state, pos.tokens, self.s.fee_bps) - 2 * self.s.tx_cost_sol
            ret = (net / pos.sol_in - 1) * 100
            if ret >= self.s.take_profit_pct:
                reason = "take_profit"
            elif ret <= -self.s.stop_loss_pct:
                reason = "stop_loss"
        if reason:
            pos.exit_reason, pos.exit_ready_ms = reason, tr.t + self.s.latency_ms

    def _request_timeouts(self, now: int) -> None:
        hold_ms = int(self.s.max_hold_s * 1000)
        for pos in self.positions.values():
            if pos.exit_ready_ms is None and now - pos.entry_t >= hold_ms:
                pos.exit_reason, pos.exit_ready_ms = "timeout", pos.entry_t + hold_ms + self.s.latency_ms

    def _fill_due(self, now: int) -> None:
        due = [(r, 0, m) for m, r in self.pending.items() if r <= now]
        due += [(p.exit_ready_ms, 1, m) for m, p in self.positions.items() if p.exit_ready_ms is not None and p.exit_ready_ms <= now]
        for ready, kind, mint in sorted(due):
            if kind == 0:
                del self.pending[mint]
                self._open(mint, ready)
            else:
                pos = self.positions[mint]
                self._close(pos, ready, pos.exit_reason)

    def _close(self, pos: Position, t: int, reason: str) -> None:
        state = self.curve[pos.mint]
        proceeds = quote_sell(state, pos.tokens, self.s.fee_bps)
        self.cash += proceeds - self.s.tx_cost_sol
        pnl = proceeds - pos.sol_in - 2 * self.s.tx_cost_sol
        del self.positions[pos.mint]
        self.closed.append(
            ClosedTrade(pos.mint, pos.entry_t, t, pos.sol_in, proceeds, pnl, pnl / pos.sol_in * 100, reason, pos.entry_mcap, state.mcap_sol)
        )
        self.equity_curve.append(self.cash + sum(p.sol_in for p in self.positions.values()))


def replay(events: list[Event], strategy: Strategy, start_sol: float = 1.0) -> EngineResult:
    engine = PaperEngine(strategy, start_sol)
    for ev in sorted(events, key=lambda e: e.t):  # stable: ties keep recorded order
        engine.on_event(ev)
    return engine.finish()
