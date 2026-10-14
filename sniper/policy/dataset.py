"""Builds labelled samples for the hold/sell model from recorded token paths (live/convert.token_paths)."""

from __future__ import annotations

from ..pump.curve import CurveState, quote_buy, quote_sell
from ..pump.events import NewToken
from .features import TokenTape, path_features, tape_from_launch
from .model import DOWN_PCT, HORIZON_S, UP_PCT, transform

GRID_S = 3.0          # one sample every few seconds of a token's life, like the engine's reviews (not only at price changes)
SIZE_SOL = 0.1        # position size whose sell value defines the label
FEE_BPS = 123.8


def token_samples(path: dict, horizon_s: float = HORIZON_S, grid_s: float = GRID_S) -> list[tuple[list[float], int, int, int]]:
    """[(transformed features, y_up, y_down, t_ms)] for one token. Samples whose horizon runs past the end of
    observation are dropped, unless the token went idle (then the price really did stay flat)."""
    c = path["create"]
    launch = NewToken(c["t"], c["mint"], c.get("creator") or "", "", "", c["dev_tokens"], c["dev_sol"], c["v_sol"], c["v_tokens"])
    obs = sorted(path["obs"], key=lambda r: r["t"])
    if not obs:
        return []
    end = path.get("end") or {}
    reason, t_end = end.get("reason"), end.get("t", obs[-1]["t"])
    if reason is None:  # still being recorded when the file was read: treat the last observation as the edge
        reason, t_end = "open", obs[-1]["t"]

    tape = tape_from_launch(launch)
    states = [(launch.t, CurveState(launch.v_sol, launch.v_tokens))] + [(r["t"], CurveState(r["v_sol"], r["v_tokens"])) for r in obs]
    times = [t for t, _ in states]

    def state_at(t: int) -> CurveState:
        lo, hi = 0, len(times) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if times[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        return states[lo][1]

    # Last moment we can label: an idle token stays flat to the end; otherwise the whole horizon must have been observed.
    last_valid = t_end if reason == "idle" else t_end - int(horizon_s * 1000)
    out, k, t = [], 1, obs[0]["t"]  # k: next observation to add to the tape
    while t <= last_valid:
        while k < len(states) and states[k][0] <= t:
            tape.add(states[k][0], states[k][1].v_sol, states[k][1].v_tokens)
            k += 1
        now = state_at(t)
        tokens = quote_buy(now, SIZE_SOL, FEE_BPS)
        fwd = (quote_sell(state_at(t + int(horizon_s * 1000)), tokens, FEE_BPS) / quote_sell(now, tokens, FEE_BPS) - 1) * 100
        out.append((transform(path_features(tape, t)), int(fwd > UP_PCT), int(fwd < -DOWN_PCT), t))
        t += int(grid_s * 1000)
    return out
