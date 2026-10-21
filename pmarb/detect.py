"""Arbitrage detectors. Each takes normalised Quotes and returns Opportunities (net > 0 only when asked to include misses)."""

from __future__ import annotations

from itertools import combinations

from .model import Opportunity, Quote, build, no_leg, yes_leg

GREATER = ("greater", "greater_or_equal")
LESS = ("less", "less_or_equal")


def sell_all(event: str, title: str, quotes: list[Quote]) -> Opportunity | None:
    """Mutually exclusive outcomes: buy NO on every one. At most one YES can win, so the set pays at least N - 1.

    Profit per set = sum(bids) - 1 - fees. Risk-free if the outcomes really are mutually exclusive (no exhaustiveness needed).
    """
    legs = [no_leg(q) for q in quotes]
    if len(quotes) < 2 or any(leg is None for leg in legs):
        return None
    return build("sell_all", event, title, legs, payout_min=len(quotes) - 1.0, risk_free=True,
                 note="mutually exclusive set; buy NO on every outcome")


def buy_all(event: str, title: str, quotes: list[Quote]) -> Opportunity | None:
    """Exactly-one-wins outcomes: buy YES on every one; pays 1. Only risk-free if the listed set is exhaustive."""
    legs = [yes_leg(q) for q in quotes]
    if len(quotes) < 2 or any(leg is None for leg in legs):
        return None
    return build("buy_all", event, title, legs, payout_min=1.0, risk_free=False,
                 note="needs the listed outcomes to be exhaustive (unverified)")


def implication(event: str, title: str, a: Quote, b: Quote) -> Opportunity | None:
    """b implies a (so P(a) >= P(b)). Buy YES a and NO b: pays at least 1. An arbitrage when bid_b > ask_a."""
    ya, nb = yes_leg(a), no_leg(b)
    if ya is None or nb is None:
        return None
    return build("implication", event, f"{title}: {b.label} implies {a.label}", [ya, nb], payout_min=1.0, risk_free=True,
                 note="threshold ladder: the stricter condition cannot be likelier")


def shape(text: str) -> str:
    """Text with digits and punctuation removed: two markets with the same shape differ only in their numbers."""
    return " ".join("".join(c for c in text.lower() if c.isalpha() or c.isspace()).split())


def ladders(event: str, title: str, quotes: list[Quote]) -> list[Opportunity]:
    """Threshold ladders: the same question asked at several thresholds. For 'above X' a higher X implies a lower X;
    for 'below X' the reverse.

    Only markets with the same strike type, the same close time, the same label shape (so the same player / team /
    asset) and the same rules text apart from numbers are compared. Anything looser matches unrelated subjects.
    """
    groups: dict[tuple, list[Quote]] = {}
    for q in quotes:
        if q.strike is not None and q.strike_type in GREATER + LESS:
            groups.setdefault((q.strike_type in GREATER, q.close_ts, shape(q.label), shape(q.rules)), []).append(q)
    out = []
    for (is_greater, *_), fam in groups.items():
        for lo, hi in combinations(sorted(fam, key=lambda q: q.strike), 2):
            if lo.strike == hi.strike:
                continue
            a, b = (lo, hi) if is_greater else (hi, lo)   # b implies a
            if (opp := implication(event, title, a, b)) is not None:
                out.append(opp)
    return out


def cross_pair(kind: str, title: str, a: Quote, b: Quote, same_resolution: bool, note: str) -> list[Opportunity]:
    """The same proposition on two venues: buy YES where it is cheap and NO where YES is dear (both directions)."""
    out = []
    for cheap, dear in ((a, b), (b, a)):
        y, n = yes_leg(cheap), no_leg(dear)
        if y is not None and n is not None:
            opp = build(kind, f"{a.event} | {b.event}", title, [y, n], payout_min=1.0, risk_free=same_resolution, note=note)
            if opp is not None:
                out.append(opp)
    return out
