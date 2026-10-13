from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import mean

from .engine import EngineResult


@dataclass(frozen=True)
class Stats:
    trades: int
    win_rate: float          # %
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float    # mean net return per trade
    expectancy_ci95: tuple[float, float] | None  # bootstrap; None when too few trades to say anything
    total_pnl_sol: float
    final_equity: float
    return_pct: float
    max_drawdown_pct: float
    exits: dict[str, int]


MIN_TRADES_FOR_CI = 20


def bootstrap_ci(returns: list[float], n: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    if len(returns) < MIN_TRADES_FOR_CI:
        return None
    rng = random.Random(seed)
    means = sorted(mean(rng.choices(returns, k=len(returns))) for _ in range(n))
    return means[int(n * 0.025)], means[int(n * 0.975) - 1]


def max_drawdown_pct(curve: list[float]) -> float:
    peak, worst = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100 if peak > 0 else 0.0)
    return worst


def summarise(r: EngineResult) -> Stats:
    rets = [t.ret_pct for t in r.closed]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    exits: dict[str, int] = {}
    for t in r.closed:
        exits[t.reason] = exits.get(t.reason, 0) + 1
    return Stats(
        trades=len(rets),
        win_rate=len(wins) / len(rets) * 100 if rets else 0.0,
        avg_win_pct=mean(wins) if wins else 0.0,
        avg_loss_pct=mean(losses) if losses else 0.0,
        expectancy_pct=mean(rets) if rets else 0.0,
        expectancy_ci95=bootstrap_ci(rets),
        total_pnl_sol=sum(t.pnl_sol for t in r.closed),
        final_equity=r.final_equity,
        return_pct=(r.final_equity / r.start_sol - 1) * 100,
        max_drawdown_pct=max_drawdown_pct(r.equity_curve),
        exits=exits,
    )
