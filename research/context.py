"""What a feature function is allowed to know: the state of the world at the decision time, and nothing later."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .data import Market, Seen, Token
from .outcomes import decision_time, tradeable

GRADUATION_V_TOKENS = 280_000_000
LAUNCH_PRICE = 30.0 / 1_073_000_000


@dataclass(frozen=True)
class Context:
    mint: str
    creator: str
    name: str
    symbol: str
    t_launch: int
    dev_pct: float
    dev_sol: float
    v_sol0: float
    v_tokens0: float
    t0: int                                       # the decision time (ms)
    delay_s: float                                # how long after the first quote we waited
    obs: tuple[tuple[int, float, float], ...]     # (t, v_sol, v_tokens) with t <= t0, oldest first; always at least one
    check: tuple[int, float, float] | None        # sell-route check (t, sol_in, sol_back) if made by t0
    market: tuple[Seen, ...]                      # every OTHER token launched by t0, as it looked at t0, oldest first

    @property
    def price_now(self) -> float:
        return self.obs[-1][1] / self.obs[-1][2]

    @property
    def price_launch(self) -> float:
        return self.v_sol0 / self.v_tokens0

    @property
    def gain_since_launch(self) -> float:
        """Price now relative to the launch price (1.0 = unchanged)."""
        return self.price_now / self.price_launch


def build_context(tok: Token, market: Market, delay_s: float) -> Context | None:
    if not tradeable(tok, delay_s):
        return None
    t0 = decision_time(tok, delay_s)
    obs = tuple(o for o in tok.obs if o[0] <= t0)
    chk = tok.check if tok.check is not None and tok.check[0] <= t0 else None
    return Context(tok.mint, tok.creator, tok.name, tok.symbol, tok.t_launch, tok.dev_pct, tok.dev_sol, tok.v_sol0, tok.v_tokens0, t0, delay_s,
                   obs, chk, tuple(market.view(t0, exclude_idx=tok.idx)))


def safe_log(x: float) -> float:
    return math.log(x) if x > 0 else float("nan")
