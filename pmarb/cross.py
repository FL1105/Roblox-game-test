"""Cross-venue hedges for matched games: the same team winning, quoted on Kalshi and on Polymarket."""

from __future__ import annotations

from dataclasses import dataclass

from . import match, venues
from .detect import cross_pair
from .model import Opportunity, Quote


@dataclass(frozen=True)
class GameRow:
    game: str
    kalshi: Quote
    poly: Quote
    best: Opportunity | None        # best hedge direction, any sign
    gross: float                    # best gross edge per set before fees (can be negative)


def price_games(pairs: list[tuple[venues.Event, dict]], progress=None) -> list[GameRow]:
    """Fresh quotes on both venues for every matched game, then the best hedge per team (taker on both sides)."""
    fees = venues.kalshi_fees()
    fresh = venues.kalshi_refresh([e.id for e, _ in pairs], fees)
    by_slug = {r["slug"]: r for _, r in pairs}
    poly = {e.id: e for e in venues.poly_snapshot(raw_events=list(by_slug.values()), market_filter=lambda e, m: m.get("sportsMarketType") == "moneyline")}
    rows = []
    for k_old, raw in pairs:
        k, p = fresh.get(k_old.id), poly.get(raw["slug"])
        if k is None or p is None:
            continue
        teams = match.team_quotes(p, raw)
        for kq in k.quotes:
            code = kq.market.rsplit("-", 1)[-1].upper()
            pq = teams.get(code)
            if pq is None:
                continue
            opps = cross_pair("cross_game", f"{k.title} [{code}]", kq, pq, same_resolution=False,
                              note="same team winning; resolution rules not compared (postponement / cancellation handling may differ)")
            gross = max([(o.legs[0].price + o.legs[1].price) for o in opps], default=None)
            rows.append(GameRow(f"{k.id} [{code}]", kq, pq, max(opps, key=lambda o: o.net) if opps else None,
                                1.0 - min((o.legs[0].price + o.legs[1].price) for o in opps) if opps else float("-inf")))
    return rows
