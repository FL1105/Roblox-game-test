"""pump.fun bonding-curve maths (constant product on *virtual* reserves).

Launch state is ~30 virtual SOL / ~1.073B virtual tokens; the curve completes (token
migrates to an AMM) when virtual tokens fall to ~279.9M. The fee rate changes over time,
so it is always a parameter, never a constant: check the current value before trusting results.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import TOTAL_SUPPLY

GRADUATION_V_TOKENS = 280_000_000  # approx: 1.073B virtual - 793.1M sellable
VIRTUAL_START_SOL = 30.0  # virtual SOL on a fresh curve; anything above it is SOL actually raised


@dataclass(frozen=True)
class CurveState:
    v_sol: float
    v_tokens: float

    @property
    def price(self) -> float:
        """SOL per token."""
        return self.v_sol / self.v_tokens

    @property
    def mcap_sol(self) -> float:
        return self.price * TOTAL_SUPPLY

    @property
    def graduated(self) -> bool:
        return self.v_tokens <= GRADUATION_V_TOKENS


def quote_buy(state: CurveState, sol_in: float, fee_bps: float) -> float:
    """Tokens received for `sol_in` SOL (fee taken from the SOL before it hits the curve)."""
    sol = sol_in * (1 - fee_bps / 10_000)
    k = state.v_sol * state.v_tokens
    return state.v_tokens - k / (state.v_sol + sol)


def quote_sell(state: CurveState, tokens_in: float, fee_bps: float) -> float:
    """SOL received for selling `tokens_in` tokens (fee taken from the proceeds)."""
    k = state.v_sol * state.v_tokens
    gross = state.v_sol - k / (state.v_tokens + tokens_in)
    return gross * (1 - fee_bps / 10_000)
