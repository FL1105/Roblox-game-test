"""Small statistics helpers (standard library only)."""

from __future__ import annotations

import math
from dataclasses import dataclass


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - half) / denom, (centre + half) / denom


def longest_run(flags: list[bool]) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def max_drawdown(pnls: list[float]) -> float:
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


@dataclass(frozen=True)
class Summary:
    n: int
    win_rate: float
    avg_cost: float       # cents per contract, price plus fee
    mean: float           # mean P&L, cents per contract
    lo: float             # 95% interval on the mean
    hi: float
    p_pos: float          # one-sided p-value for "mean > 0" (normal approximation)
    roi: float            # total P&L / total cost
    drawdown: float       # cents per contract, cumulative, in time order
    losing_streak: int
    avg_win: float
    avg_loss: float


def summarize(trades) -> Summary:
    """Summarise trades in time order. Each needs .price, .fee, .pnl (cents per contract) and .win."""
    n = len(trades)
    if n == 0:
        return Summary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0, 0.0, 0.0)
    pnls = [t.pnl for t in trades]
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n)
    wins = [t.pnl for t in trades if t.win]
    losses = [t.pnl for t in trades if not t.win]
    cost = sum(t.price + t.fee for t in trades)
    return Summary(
        n, len(wins) / n, cost / n, mean, mean - 1.96 * se, mean + 1.96 * se,
        1.0 - norm_cdf(mean / se) if se > 0 else (0.0 if mean > 0 else 1.0),
        sum(pnls) / cost if cost else 0.0, max_drawdown(pnls), longest_run([not t.win for t in trades]),
        sum(wins) / len(wins) if wins else 0.0, sum(losses) / len(losses) if losses else 0.0,
    )
