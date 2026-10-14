"""Explainable sell/hold rules that look at momentum and net buying, not just a fixed target.

Each rule is a hypothesis about pump.fun price paths, not a fact: sweep/ablate them on recorded real data before
trusting any. They only decide when to take profit or cut a fading position; the engine keeps its hard guards
(stop loss, creator sold, graduated, max hold) regardless of the policy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SmartParams:
    tp_floor: float = 30.0        # net %: from here, sell as soon as buying stalls (momentum <= 0 over 5 s)
    tp_cap: float = 150.0         # net %: sell unconditionally
    arm_ret: float = 15.0         # net %: the volatility trail arms once the position has been this far in profit
    trail_k: float = 3.0          # trail width = k x recent volatility (per-move std of log price)
    trail_min: float = 0.05       # ... clamped between 5% and 25% of the best value reached
    trail_max: float = 0.25
    stall_idle_s: float = 30.0    # in profit and nothing has traded for this long: take it
    flow_exit: float = 0.04       # net SOL outflow over 15 s, as a share of the curve's SOL, that counts as dumping
    use_stall: bool = True
    use_flow: bool = True
    use_trail: bool = True
    use_momentum_tp: bool = True


def decide(p: SmartParams, ret_pct: float, peak_ret_pct: float, f: dict[str, float]) -> str | None:
    """`ret_pct`/`peak_ret_pct`: net return now and the best seen while held. `f`: path features (features.py)."""
    if ret_pct >= p.tp_cap:
        return "take_profit"
    if p.use_momentum_tp and ret_pct >= p.tp_floor and f["mom_5"] <= 0:
        return "profit_stall"
    if p.use_trail and peak_ret_pct >= p.arm_ret:
        width = min(max(p.trail_k * f["vol_30"], p.trail_min), p.trail_max)
        peak_val, now_val = 1 + peak_ret_pct / 100, 1 + ret_pct / 100
        if now_val <= peak_val * (1 - width):
            return "smart_trail"
    if p.use_stall and ret_pct > 0 and f["idle_s"] >= p.stall_idle_s:
        return "stalled"
    if p.use_flow and ret_pct > 0 and f["flow_15"] < -p.flow_exit * max(f["liq_sol"] + 30.0, 1.0):
        return "sell_pressure"
    return None
