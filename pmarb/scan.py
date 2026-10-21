"""Run every detector over a snapshot and summarise what it finds."""

from __future__ import annotations

from collections import Counter

from .detect import buy_all, ladders, sell_all
from .model import Opportunity
from .venues import Event


def evaluate(events: list[Event]) -> list[Opportunity]:
    """Every candidate set the detectors can price, profitable or not, so the near misses can be counted too."""
    out: list[Opportunity] = []
    for e in events:
        quotes = list(e.quotes)
        if e.exclusive and len(quotes) >= 2:
            for opp in (sell_all(e.id, e.title, quotes), buy_all(e.id, e.title, quotes)):
                if opp is not None:
                    out.append(opp)
        if len(quotes) >= 2 and any(q.strike is not None for q in quotes):
            out += ladders(e.id, e.title, quotes)
    return out


def summarise(opps: list[Opportunity]) -> dict:
    """Counts by kind: priced candidates, how many are profitable after fees, and the best few."""
    by_kind: dict[str, list[Opportunity]] = {}
    for o in opps:
        by_kind.setdefault(o.kind, []).append(o)
    out = {}
    for kind, rows in by_kind.items():
        rows.sort(key=lambda o: o.net, reverse=True)
        profitable = [o for o in rows if o.net > 0]
        out[kind] = {"priced": len(rows), "profitable": len(profitable), "profit_total": sum(o.net for o in profitable), "top": profitable[:10]}
    return out


def describe(o: Opportunity) -> str:
    legs = "; ".join(f"{l.side.upper()} {l.venue}:{l.label[:28]} @{l.price:.3f}x{l.size:g}" for l in o.legs[:6])
    more = f" (+{len(o.legs) - 6} more)" if len(o.legs) > 6 else ""
    return (f"{o.kind:12s} net ${o.net:8.2f} on {o.qty:g} sets (roi {100 * o.roi:5.1f}%, fees ${o.fees:.2f}) "
            f"{'RISK-FREE' if o.risk_free else 'UNVERIFIED'} | {o.event} | {o.title[:50]} | {legs}{more}")
