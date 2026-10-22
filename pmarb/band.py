"""The 'sell the band' test of PREREGISTERED_band.txt: buy YES on one venue and NO on the other for the same BTC 15-minute window."""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass

from kalshi_btc15 import api

from . import venues
from .model import Fee

KFEE = Fee(0.07)          # per-set fees are small and unrounded here; the test is one set per window
PFEE = Fee(0.07)
ENTRY_S = 300             # seconds before close
MAX_AGE_S = 150           # a Polymarket price older than this is not used


@dataclass(frozen=True)
class Option:
    yes_on: str
    no_on: str
    net: float            # modeled net per set (payoff 1 assumed), after taker fees on both legs


def options(k_bid: float, k_ask: float, p_mid: float, half: float) -> list[Option]:
    """Both orientations, priced at Kalshi's real bid/ask and Polymarket's price +/- a half-spread."""
    p_bid, p_ask = p_mid - half, p_mid + half
    out = []
    for yes_on, no_on, yes_price, no_price in (("kalshi", "poly", k_ask, 1.0 - p_bid), ("poly", "kalshi", p_ask, 1.0 - k_bid)):
        fy = (KFEE if yes_on == "kalshi" else PFEE).total(yes_price, 1)
        fn = (KFEE if no_on == "kalshi" else PFEE).total(no_price, 1)
        out.append(Option(yes_on, no_on, 1.0 - yes_price - no_price - fy - fn))
    return out


def payoff(opt: Option, kalshi_up: bool, poly_up: bool) -> int:
    """YES pays if its venue resolved Up, NO pays if its venue resolved Down."""
    up = {"kalshi": kalshi_up, "poly": poly_up}
    return int(up[opt.yes_on]) + int(not up[opt.no_on])


def poly_price_at(history: list[dict], ts: int) -> float | None:
    """Last Polymarket history price at or before ts, no older than MAX_AGE_S."""
    pts = [h for h in history if h["t"] <= ts]
    if not pts or ts - pts[-1]["t"] > MAX_AGE_S:
        return None
    return float(pts[-1]["p"])


def score(rows: list[dict], threshold: float) -> dict:
    """rows: {net_best: Option, realised: dict payoff->count...}. Mean realised net per set over trades with modeled net >= threshold."""
    taken = [r for r in rows if r["opt"].net >= threshold]
    if not taken:
        return {"n": 0}
    nets = [r["opt"].net + r["payoff"] - 1.0 for r in taken]          # realised = modeled net + (payoff - 1)
    n = len(nets); mean = statistics.mean(nets)
    se = statistics.stdev(nets) / math.sqrt(n) if n > 1 else float("nan")
    pay = {k: sum(r["payoff"] == k for r in taken) for k in (0, 1, 2)}
    z = mean / se if se and se == se else 0.0
    return {"n": n, "mean": mean, "se": se, "lo": mean - 1.96 * se, "hi": mean + 1.96 * se, "p_pos": 0.5 * math.erfc(z / math.sqrt(2)),
            "payoffs": pay, "modeled": statistics.mean(r["opt"].net for r in taken)}


def collect(windows: list[list], cache_path: str, say=print) -> dict[int, list[dict]]:
    """Polymarket Up-token price history for each [close_ts, kalshi_up, poly_up] window (cached on disk)."""
    try:
        cache = {int(k): v for k, v in json.load(open(cache_path)).items()}
    except (OSError, ValueError):
        cache = {}
    gate = api.new_gate(0.1)
    for i, (close, _k, _p) in enumerate(windows, 1):
        if close in cache:
            continue
        start = close - 900
        try:
            m = api.get_json(f"{venues.GAMMA}/events", {"slug": f"btc-updown-15m-{start}"}, gate=gate)[0]["markets"][0]
            tok = json.loads(m["clobTokenIds"])[json.loads(m["outcomes"]).index("Up")]
            h = api.get_json(f"{venues.CLOB}/prices-history", {"market": tok, "startTs": start, "endTs": close, "fidelity": 1}, gate=gate)
            cache[close] = h.get("history", [])
        except (api.ApiError, IndexError, KeyError, ValueError):
            cache[close] = []
        if i % 100 == 0:
            say(f"  history {i}/{len(windows)}")
            json.dump(cache, open(cache_path, "w"))
    json.dump(cache, open(cache_path, "w"))
    return cache


def run(windows_path: str, cache_path: str, say=print) -> str:
    """Score the pre-registered rule once and return the report text."""
    windows = json.load(open(windows_path))
    cache = {int(k): v for k, v in json.load(open(cache_path)).items()}
    by_close = {m.close_ts: m for m in api.settled_markets()}
    lines, skipped = [], {"no Kalshi quote": 0, "no fresh Polymarket price": 0, "price outside 3-97c": 0}
    prepared = []
    for close, k_up, p_up in windows:
        m = by_close.get(close)
        candle = next((c for c in api.candles(api.SERIES, m) if c.end_ts == close - ENTRY_S), None) if m else None
        if candle is None or candle.bid is None or candle.ask is None:
            skipped["no Kalshi quote"] += 1
            continue
        pm = poly_price_at(cache.get(close, []), close - ENTRY_S)
        if pm is None:
            skipped["no fresh Polymarket price"] += 1
            continue
        if not (0.03 <= pm <= 0.97 and 0.03 <= (candle.bid + candle.ask) / 200 <= 0.97):
            skipped["price outside 3-97c"] += 1
            continue
        prepared.append((candle.bid / 100, candle.ask / 100, pm, k_up, p_up, close))
    lines.append(f"{len(windows)} windows; usable {len(prepared)}; skipped {skipped}")
    gaps = [abs((b + a) / 2 - pm) for b, a, pm, *_ in prepared]
    lines.append(f"|Kalshi mid - Polymarket price| at {ENTRY_S}s left: mean {100 * statistics.mean(gaps):.1f}c, median {100 * statistics.median(gaps):.1f}c")
    for half in (0.005, 0.010):
        rows = []
        for b, a, pm, k_up, p_up, close in prepared:
            best = max(options(b, a, pm, half), key=lambda o: o.net)
            rows.append({"opt": best, "payoff": payoff(best, k_up, p_up), "close": close})
        lines.append(f"\nhalf-spread {100 * half:.1f}c on Polymarket" + ("  <== PRIMARY" if half == 0.005 else "  (sensitivity)"))
        for thr in (0.0, 0.02, 0.05, 0.10):
            s = score(rows, thr)
            if s["n"] == 0:
                lines.append(f"  modeled net >= {100 * thr:.0f}c: no trades"); continue
            lines.append(f"  modeled net >= {100 * thr:2.0f}c{' (pre-registered)' if thr == 0.02 and half == 0.005 else ''}: {s['n']:4d} trades, modeled {100 * s['modeled']:+.1f}c, "
                         f"REALISED {100 * s['mean']:+6.2f}c/set [{100 * s['lo']:+.2f}, {100 * s['hi']:+.2f}], p(>0)={s['p_pos']:.3f}, payoffs 0/1/2 = {s['payoffs'][0]}/{s['payoffs'][1]}/{s['payoffs'][2]}")
    return "\n".join(lines)
