"""What actually happens to real launches? Descriptive statistics from a raw recording.

For each priceable token the question is: if 0.1 SOL had been spent at the first moment the token could be quoted,
what would holding have looked like, net of both fees? (No latency or tx costs here; the paper engine adds those.)
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter

from ..pump.curve import CurveState, quote_buy, quote_sell
from .convert import read_raw, token_paths

SIZE = 0.1
FEE_BPS = 123.8
HORIZONS_S = (15, 30, 60, 120, 300)


def token_summary(p: dict) -> dict | None:
    c, obs = p["create"], sorted(p["obs"], key=lambda r: r["t"])
    if not obs:
        return None
    first = obs[0]
    st0 = CurveState(first["v_sol"], first["v_tokens"])
    tokens = quote_buy(st0, SIZE, FEE_BPS)
    cost = SIZE

    def ret(st: CurveState) -> float:
        return (quote_sell(st, tokens, FEE_BPS) / cost - 1) * 100

    path = [(r["t"], ret(CurveState(r["v_sol"], r["v_tokens"]))) for r in obs]
    t0 = first["t"]

    def at(sec: float) -> float | None:
        target = t0 + sec * 1000
        last = None
        for t, r in path:
            if t <= target:
                last = r
        end = p.get("end") or {}
        if target > end.get("t", path[-1][0]) and end.get("reason") not in ("idle",):
            return None  # not observed that long
        return last

    # first passage of +30% vs -15% (net), in observation order
    outcome = "neither"
    for _, r in path:
        if r >= 30:
            outcome = "tp30"; break
        if r <= -15:
            outcome = "sl15"; break
    d = (c["dev_tokens"] / 1e9) * 100
    return {"mint": c["mint"], "dev_pct": d, "first_delay_s": (t0 - c["t"]) / 1000, "n_moves": len(obs) - 1, "peak": max(r for _, r in path),
            "trough": min(r for _, r in path), "at": {h: at(h) for h in HORIZONS_S}, "outcome": outcome,
            "end": (p.get("end") or {}).get("reason", "open"), "entry_gain_pct": (st0.price / (30.0 / 1.073e9) - 1) * 100}


def report(paths: dict[str, dict]) -> str:
    rows = [r for r in (token_summary(p) for p in paths.values()) if r]
    out = [f"{len(paths)} priceable tokens, {len(rows)} with at least one observation"]
    if not rows:
        return out[0]
    out.append("how tracking ended: " + str(dict(Counter(r["end"] for r in rows))))
    out.append(f"first quotable {statistics.median(r['first_delay_s'] for r in rows):.1f}s after launch (median)")
    alive = [r for r in rows if r["n_moves"] > 0]
    out.append(f"price moved at least once after the first quote: {len(alive)}/{len(rows)} ({len(alive) / len(rows) * 100:.0f}%)")
    out.append(f"already above launch price at first quote by >10%: {sum(r['entry_gain_pct'] > 10 for r in rows)}/{len(rows)}")
    out.append("")
    out.append("BLIND ENTRY at the first quote, 0.1 SOL, net of fees. Peak = best moment in hindsight, not achievable:")
    peaks = sorted(r["peak"] for r in rows)
    for x in (10, 30, 50, 100):
        out.append(f"  peak net gain >= +{x}%: {sum(p >= x for p in peaks)}/{len(rows)} ({sum(p >= x for p in peaks) / len(rows) * 100:.0f}%)")
    out.append("  held for a fixed time then sold (tokens still observed at that time):")
    for h in HORIZONS_S:
        v = [r["at"][h] for r in rows if r["at"][h] is not None]
        if len(v) >= 5:
            out.append(f"    after {h:>3}s: n={len(v):4}  mean {statistics.mean(v):+6.1f}%  median {statistics.median(v):+6.1f}%  winners {sum(x > 0 for x in v) / len(v) * 100:3.0f}%")
    o = Counter(r["outcome"] for r in rows)
    out.append(f"  first to be hit, +30% or -15% net: {dict(o)}")
    hit = o["tp30"] / max(o["tp30"] + o["sl15"], 1)
    out.append(f"  of tokens that hit either, share hitting +30% first: {hit * 100:.0f}%  (a coin flip would be 50%; with costs, break-even needs {15 / 45 * 100:.0f}%+ for a 30/-15 rule)")
    return "\n".join(out)


if __name__ == "__main__":
    print(report(token_paths([r for path in sys.argv[1:] for r in read_raw(path)])))
