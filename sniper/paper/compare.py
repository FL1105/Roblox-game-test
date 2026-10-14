"""Head-to-head strategy comparison on recorded events, with an honest holdout.

Per-trade quality is what matters when comparing policies, so every variant gets the same generous bookkeeping:
a big bankroll, many concurrent positions and fixed 0.1 SOL size, so no trade is skipped for lack of cash.
Fees, the decision-to-fill delay and the per-transaction cost are all still charged.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace

from ..policy.model import HoldModel, ModelExit
from ..pump.events import Event, NewToken
from .engine import PaperEngine, Strategy
from .stats import MIN_TRADES_FOR_CI, bootstrap_ci

BANKROLL = 1000.0


def base_strategy(tx_cost_sol: float = 0.003, latency_ms: int = 800) -> Strategy:
    return Strategy(size_sol=0.1, size_caution_sol=0.1, max_open=50, tx_cost_sol=tx_cost_sol, latency_ms=latency_ms,
                    take_profit_pct=30.0, stop_loss_pct=15.0, max_hold_s=300.0, allowed_verdicts=frozenset({"PASS", "CAUTION"}),
                    max_dev_buy_pct=100.0, max_entry_mcap_sol=1000.0, slippage_pct=50.0, exit_on_creator_sell=True)


def variants(base: Strategy, have_model: bool) -> dict[str, Strategy]:
    out = {
        "snipe + fixed targets (30% / -15%)": base,
        "snipe + smart exit": replace(base, exit_mode="smart"),
        "confirm + fixed targets": replace(base, entry_mode="confirm"),
        "confirm + smart exit": replace(base, entry_mode="confirm", exit_mode="smart"),
    }
    if have_model:
        out["confirm + learned model"] = replace(base, entry_mode="confirm", exit_mode="model")
        out["snipe + learned model"] = replace(base, exit_mode="model")
    return out


@dataclass(frozen=True)
class Row:
    name: str
    trades: int
    win_rate: float
    mean_ret: float
    ci: tuple[float, float] | None
    pnl_sol: float
    reasons: dict[str, int]


def run(events: list[Event], strategy: Strategy, model: ModelExit | None = None, name: str = "") -> Row:
    eng = PaperEngine(strategy, BANKROLL, model_policy=model)
    for ev in sorted(events, key=lambda e: e.t):
        eng.on_event(ev)
    res = eng.finish()
    rets = [t.ret_pct for t in res.closed]
    reasons: dict[str, int] = {}
    for t in res.closed:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return Row(name, len(rets), sum(r > 0 for r in rets) / len(rets) * 100 if rets else 0.0, statistics.mean(rets) if rets else 0.0,
               bootstrap_ci(rets), sum(t.pnl_sol for t in res.closed), reasons)


def split_by_launch(events: list[Event], train_frac: float = 0.6) -> tuple[list[Event], list[Event]]:
    launches = sorted((e for e in events if isinstance(e, NewToken)), key=lambda e: e.t)
    cut = int(len(launches) * train_frac)
    train = {e.mint for e in launches[:cut]}
    test = {e.mint for e in launches[cut:]}
    return [e for e in events if e.mint in train], [e for e in events if e.mint in test]


def fmt_row(r: Row) -> str:
    ci = f"[{r.ci[0]:+5.1f},{r.ci[1]:+5.1f}]" if r.ci else "  (n<%d)    " % MIN_TRADES_FOR_CI
    return f"{r.name:38} {r.trades:5} {r.win_rate:5.0f}% {r.mean_ret:+7.2f}%  {ci}  {r.pnl_sol:+8.3f}"


def compare(events: list[Event], model: HoldModel | None = None, tx_cost_sol: float = 0.003, latency_ms: int = 800, model_sell_margin: float = 0.1) -> str:
    from ..policy.model import ModelParams

    mp = ModelExit(model, ModelParams(sell_margin=model_sell_margin)) if model else None
    strategies = variants(base_strategy(tx_cost_sol, latency_ms), model is not None)
    n_tokens = sum(isinstance(e, NewToken) for e in events)
    lines = [f"{n_tokens} real launches | tx cost {tx_cost_sol} SOL per side | decision-to-fill delay {latency_ms} ms | 0.1 SOL per trade",
             f"{'strategy':38} {'trades':>5} {'win':>6} {'mean ret':>8}  {'95% CI of mean':>14}  {'P&L SOL':>8}"]
    for name, s in strategies.items():
        lines.append(fmt_row(run(events, s, mp, name)))
    return "\n".join(lines)
