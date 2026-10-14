"""Turns a token's price path into the signals a sell/hold decision can use.

Everything here is computed from what the quote feed (or any trade feed) provides: when the curve reserves changed
and to what. No trader identities, so nothing here knows who bought. Net SOL flow through the curve is the
cleanest buy/sell-pressure measure available from reserves alone.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field

from ..pump.curve import VIRTUAL_START_SOL
from ..pump.events import TOTAL_SUPPLY

LAUNCH_V_TOKENS = 1_073_000_000.0
GRADUATION_SOL = 85.0  # SOL raised when the curve completes

FEATURES = [
    "age_s", "liq_sol", "log_ret_launch", "mom_5", "mom_15", "mom_45", "flow_15", "flow_60", "trades_15", "trades_60",
    "idle_s", "up_frac_30", "dd_peak", "since_peak_s", "progress", "vol_30", "dev_pct",
]


@dataclass
class TokenTape:
    launch_t: int
    dev_pct: float = 0.0
    launch_price: float = VIRTUAL_START_SOL / LAUNCH_V_TOKENS
    ts: list[int] = field(default_factory=list)
    vs: list[float] = field(default_factory=list)
    vt: list[float] = field(default_factory=list)
    peak_price: float = 0.0
    peak_t: int = 0

    def add(self, t: int, v_sol: float, v_tokens: float) -> None:
        """Append an observation; repeats of an unchanged state are ignored."""
        if self.vt and self.vt[-1] == v_tokens and self.vs[-1] == v_sol:
            return
        self.ts.append(t); self.vs.append(v_sol); self.vt.append(v_tokens)
        price = v_sol / v_tokens
        if price >= self.peak_price:
            self.peak_price, self.peak_t = price, t

    def price(self, i: int) -> float:
        return self.vs[i] / self.vt[i]

    def index_at(self, t: int) -> int:
        """Index of the last observation at or before t (-1 if none)."""
        return bisect_right(self.ts, t) - 1

    def price_at(self, t: int) -> float:
        i = self.index_at(t)
        return self.launch_price if i < 0 else self.price(i)

    def vsol_at(self, t: int) -> float:
        i = self.index_at(t)
        return VIRTUAL_START_SOL if i < 0 else self.vs[i]


def tape_from_launch(launch) -> TokenTape:
    """Start a tape from a NewToken event (the exact launch state is the first observation)."""
    tape = TokenTape(launch_t=launch.t, dev_pct=launch.dev_tokens / TOTAL_SUPPLY * 100)
    tape.launch_price = launch.v_sol / launch.v_tokens
    tape.add(launch.t, launch.v_sol, launch.v_tokens)
    return tape


def path_features(tape: TokenTape, now: int | None = None) -> dict[str, float]:
    """Signals describing the token as of `now` (default: its latest observation). Needs at least one observation."""
    last = len(tape.ts) - 1
    now = tape.ts[last] if now is None else now
    price, vsol = tape.price(last), tape.vs[last]

    def mom(w_s: float) -> float:
        return math.log(price / tape.price_at(now - int(w_s * 1000)))

    def flow(w_s: float) -> float:
        return vsol - tape.vsol_at(now - int(w_s * 1000))

    def trades(w_s: float) -> int:
        return sum(1 for t in reversed(tape.ts) if t > now - int(w_s * 1000) and t > tape.launch_t) if tape.ts else 0

    recent = [i for i in range(max(0, last - 40), last + 1) if tape.ts[i] > now - 30_000 and i > 0]
    ups = sum(1 for i in recent if tape.price(i) > tape.price(i - 1))
    downs = sum(1 for i in recent if tape.price(i) < tape.price(i - 1))
    rets = [math.log(tape.price(i) / tape.price(i - 1)) for i in recent]
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    else:
        vol = 0.0
    return {
        "age_s": (now - tape.launch_t) / 1000,
        "liq_sol": vsol - VIRTUAL_START_SOL,
        "log_ret_launch": math.log(price / tape.launch_price),
        "mom_5": mom(5), "mom_15": mom(15), "mom_45": mom(45),
        "flow_15": flow(15), "flow_60": flow(60),
        "trades_15": float(trades(15)), "trades_60": float(trades(60)),
        "idle_s": (now - tape.ts[last]) / 1000,
        "up_frac_30": ups / (ups + downs) if ups + downs else 0.5,
        "dd_peak": math.log(price / tape.peak_price),
        "since_peak_s": (now - tape.peak_t) / 1000,
        "progress": min(max((vsol - VIRTUAL_START_SOL) / GRADUATION_SOL, 0.0), 1.0),
        "vol_30": vol,
        "dev_pct": tape.dev_pct,
    }


def vector(feats: dict[str, float]) -> list[float]:
    return [feats[name] for name in FEATURES]
