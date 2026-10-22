"""Lead-lag and persistence of cross-venue gaps in a concurrent three-venue log (pmarb btc3)."""

from __future__ import annotations

import statistics


def mid(row: dict, venue: str) -> float:
    q = row["quotes"][venue]
    return (q["bid"] + q["ask"]) / 2.0


def lagged_gap(rows: list[dict], a: str, b: str, lag_polls: int, max_step_s: float = 9.0) -> tuple[float, int]:
    """Mean |mid_a(t) - mid_b(t - lag)| over polls in the same window; a small value at a positive lag means `a` follows `b`."""
    rows = sorted(rows, key=lambda r: r["t"])
    diffs = []
    for earlier, later in zip(rows, rows[lag_polls:]):
        if earlier["close"] == later["close"] and later["t"] - earlier["t"] <= max_step_s * max(lag_polls, 1):
            diffs.append(abs(mid(later, a) - mid(earlier, b)))
    return (statistics.mean(diffs) if diffs else float("nan")), len(diffs)


def profitable_runs(rows: list[dict], max_gap_s: float = 15.0) -> list[int]:
    """Lengths of runs of consecutive polls in which the best hedge is profitable after fees."""
    runs, cur, prev_t = [], 0, None
    for r in sorted(rows, key=lambda r: r["t"]):
        if prev_t is not None and r["t"] - prev_t > max_gap_s and cur:
            runs.append(cur)
            cur = 0
        if r["best"] is not None and r["best"]["net"] > 0:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
        prev_t = r["t"]
    if cur:
        runs.append(cur)
    return runs


def first_sight_entries(rows: list[dict], max_gap_s: float = 15.0) -> list[dict]:
    """The first poll of each profitable run: what a trader watching in real time would see first."""
    out, prev_pos, prev_t = [], False, None
    for r in sorted(rows, key=lambda r: r["t"]):
        pos = r["best"] is not None and r["best"]["net"] > 0
        if pos and (not prev_pos or (prev_t is not None and r["t"] - prev_t > max_gap_s)):
            out.append(r)
        prev_pos, prev_t = pos, r["t"]
    return out
