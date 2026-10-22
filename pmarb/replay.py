"""Conservative replay of a concurrent three-venue log (PREREGISTERED_replay.txt): execute one poll late, at a fraction of the displayed size."""

from __future__ import annotations

from .btc3 import hedge_total
from .leadlag import first_sight_entries
from .model import Fee

KALSHI_FEE = Fee(0.07, 1.0, cent_round=True)
POLY_FEE = Fee(0.07)
FEES = {"kalshi": KALSHI_FEE, "poly": POLY_FEE, "limitless": None}


def usable(r: dict) -> bool:
    q = r["quotes"]
    if len(q) < 3 or not 30 <= r["left_s"] <= 840:
        return False
    return all(v["bid"] is not None and v["ask"] is not None and 0.03 <= (v["bid"] + v["ask"]) / 2 <= 0.97 for v in q.values())


def replay(rows: list[dict], delay_polls: int = 1, fill: float = 0.5, max_gap_s: float = 15.0) -> tuple[list[dict], int]:
    """Signals = first poll of each profitable run. Each is executed `delay_polls` later in the same orientation at that poll's
    prices, at `fill` x the displayed size; dropped if no longer profitable. Returns (executed trades, number of signals)."""
    rows = sorted((r for r in rows if usable(r)), key=lambda r: r["t"])
    index = {r["t"]: i for i, r in enumerate(rows)}
    signals = first_sight_entries(rows, max_gap_s)
    out = []
    for s in signals:
        i = index[s["t"]] + delay_polls
        if i >= len(rows) or rows[i]["close"] != s["close"] or rows[i]["t"] - s["t"] > max_gap_s * delay_polls:
            continue
        e, b = rows[i], s["best"]
        h = hedge_total(b["yes_on"], e["quotes"][b["yes_on"]], b["no_on"], e["quotes"][b["no_on"]], FEES[b["yes_on"]], FEES[b["no_on"]], fill)
        if h and h["net"] > 0:
            out.append({**h, "close": e["close"], "t": e["t"], "signal_t": s["t"]})
    return out, len(signals)


def realised(trades: list[dict], up: dict[int, dict[str, bool]]) -> dict:
    """Score executed trades against recorded outcomes: up[close][venue] is True when that venue resolved Up."""
    pay, modeled, real, by_window = {0: 0, 1: 0, 2: 0}, 0.0, 0.0, {}
    for t in trades:
        u = up.get(t["close"])
        if u is None:
            continue
        payoff = int(u[t["yes_on"]]) + int(not u[t["no_on"]])
        r = t["net"] + t["sets"] * (payoff - 1)
        pay[payoff] += 1; modeled += t["net"]; real += r
        w = by_window.setdefault(t["close"], [0, 0.0, 0.0]); w[0] += 1; w[1] += t["net"]; w[2] += r
    return {"trades": sum(pay.values()), "payoffs": pay, "modeled": modeled, "realised": real, "by_window": by_window}
