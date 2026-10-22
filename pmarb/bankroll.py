"""What a given bankroll could have captured from a watch log, with capital locked until the games end."""

from __future__ import annotations

from collections import defaultdict

from .model import Fee


def directions(r: dict) -> list[tuple[float, float, float]] | None:
    """The two possible hedges for one logged pair as (gross per set, Kalshi leg price, Polymarket leg price):
    0 = Kalshi YES + Polymarket NO, 1 = Polymarket YES + Kalshi NO."""
    if None in (r["k_bid"], r["k_ask"], r["p_bid"], r["p_ask"]):
        return None
    return [(r["p_bid"] - r["k_ask"], r["k_ask"], 1.0 - r["p_bid"]), (r["k_bid"] - r["p_ask"], 1.0 - r["k_bid"], r["p_ask"])]


def identify(r: dict) -> tuple[int, float] | None:
    """For a logged profitable observation: which hedge it was and the combined fee multiplier (on Kalshi's 0.07 schedule)
    implied by the fees recorded at the logged size. The direction is the one whose gross minus logged fees equals the logged net."""
    if not r.get("net") or r["net"] <= 0 or not r.get("qty"):
        return None
    for i, (gross, pk, _pp) in enumerate(directions(r) or []):
        if abs(r["qty"] * gross - r["fees"] - r["net"]) < 0.02:
            raw = 0.07 * r["qty"] * pk * (1.0 - pk)
            return i, (r["fees"] / raw if raw else 1.0)
    return None


def simulate(rows: list[dict], bankroll: float, min_sets: int = 5) -> list[tuple[int, str, int, float]]:
    """Take each profitable episode the first poll it appears, sized to the displayed depth and the capital left on each venue
    (half the bankroll each), with that capital locked for the rest of the log. Returns [(time, label, sets, net)].

    A ceiling: it assumes both legs fill instantly at the displayed prices and sizes.
    """
    by_t: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "game":
            by_t[r["t"]].append(r)
    pool_k = pool_p = bankroll / 2.0
    prev: set[str] = set()
    taken = []
    for t in sorted(by_t):
        now: set[str] = set()
        for r in by_t[t]:
            found = identify(r)
            if found is None:
                continue
            now.add(r["label"])
            if r["label"] in prev:
                continue
            d, mult = found
            gross, pk, pp = directions(r)[d]
            q = min(int(r["qty"]), int(pool_k / pk), int(pool_p / pp))
            if q < min_sets:
                continue
            net = q * gross - Fee(0.07 * mult, 1.0, cent_round=True).total(pk, q)
            if net > 0:
                pool_k -= q * pk
                pool_p -= q * pp
                taken.append((t, r["label"], q, net))
        prev = now
    return taken
