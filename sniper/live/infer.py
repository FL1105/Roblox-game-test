"""Recover a pump.fun curve's exact reserves from Jupiter buy quotes.

A pump.fun curve is constant-product on virtual reserves (S sol, T tokens) with a fee f taken from the SOL in:
    tokens_out(s) = T * f*s / (S + f*s)   =>   1/tokens = (S/(T*f)) * (1/s) + 1/T
That is a straight line in 1/s, so two quotes fix (T, S/f) and a third quote checks the fit. A trade landing
between the quotes breaks the fit, which is how torn snapshots are detected and discarded.
Measured on live tokens: a fit on 4 sizes predicted a 5th quote to 0.0002%, and an untouched token implied a fee of 1.24%.
"""

from __future__ import annotations

from dataclasses import dataclass

FEE = 0.01238  # pump.fun's total fee on the SOL side, measured on live quotes (k ratio 0.99987 over 17 tokens)
TOKEN_DECIMALS = 1e6
SNAPSHOT_SOL = (0.02, 0.3, 1.0)  # two sizes to solve, the third to verify
MAX_CHECK_ERROR = 1e-4  # a third quote that misses the fit by more than 0.01% means the state moved mid-snapshot


@dataclass(frozen=True)
class Reserves:
    v_sol: float
    v_tokens: float
    s_over_f: float
    check_error: float  # relative miss of the verification quote


def infer_reserves(quotes: list[tuple[float, float]], fee: float = FEE) -> Reserves | None:
    """`quotes` = [(sol_in, tokens_out), ...] with tokens in whole tokens. Needs three; returns None if unusable."""
    if len(quotes) < 3 or any(s <= 0 or t <= 0 for s, t in quotes):
        return None
    (s1, t1), (s2, t2), (s3, t3) = quotes[:3]
    x1, x2, y1, y2 = 1 / s1, 1 / s2, 1 / t1, 1 / t2
    if x1 == x2:
        return None
    m = (y1 - y2) / (x1 - x2)
    c = y1 - m * x1
    if m <= 0 or c <= 0:
        return None  # not a curve we understand (e.g. the token left the bonding curve)
    tokens = 1 / c
    s_over_f = m * tokens
    err = tokens * s3 / (s_over_f + s3) / t3 - 1
    return Reserves(v_sol=s_over_f * (1 - fee), v_tokens=tokens, s_over_f=s_over_f, check_error=err)


def is_consistent(r: Reserves | None) -> bool:
    return r is not None and abs(r.check_error) <= MAX_CHECK_ERROR


def reserves_from_probe(k: float, sol_in: float, tokens_out: float, fee: float = FEE) -> tuple[float, float] | None:
    """(v_sol, v_tokens) from ONE buy quote, given the curve's invariant k = v_sol * v_tokens.

    k never changes on a pump.fun curve (fees leave the reserves), and the launch message gives it exactly, so
    tokens_out = k*f*s / (v_sol*(v_sol + f*s)) is a quadratic in v_sol:  v_sol^2 + a*v_sol - k*a/tokens_out = 0, a = f*s.
    """
    if k <= 0 or sol_in <= 0 or tokens_out <= 0:
        return None
    a = (1 - fee) * sol_in
    v_sol = (-a + (a * a + 4 * k * a / tokens_out) ** 0.5) / 2
    return v_sol, k / v_sol
