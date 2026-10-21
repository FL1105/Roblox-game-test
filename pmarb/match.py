"""Match the same proposition across venues. Sports games are matched exactly (date plus the two team codes);
nothing here guesses from fuzzy titles, because a wrong match looks exactly like a huge arbitrage."""

from __future__ import annotations

import re
from datetime import datetime

from .model import Quote
from .venues import Event

KALSHI_GAME = re.compile(r"^KX([A-Z0-9]+?)GAME-(\d\d)([A-Z]{3})(\d\d)([A-Z0-9]+)$")
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def kalshi_game_key(event: Event) -> tuple[str, str, frozenset] | None:
    """(league, ISO date, {team codes}) for a Kalshi two-team game-winner event, from its ticker and market suffixes."""
    m = KALSHI_GAME.match(event.id)
    if not m or event.id.startswith("KXMVE"):
        return None
    league, yy, mon, dd, _ = m.groups()
    if mon not in MONTHS:
        return None
    codes = frozenset(q.market.rsplit("-", 1)[-1].upper() for q in event.quotes)
    if len(codes) != 2:
        return None
    return league, f"20{yy}-{MONTHS[mon]:02d}-{dd}", codes


def poly_game_key(raw: dict) -> tuple[str, frozenset] | None:
    """(ISO date, {team abbreviations}) for a Polymarket sports event with exactly two teams."""
    teams = raw.get("teams") or []
    if len(teams) != 2 or not raw.get("eventDate"):
        return None
    return raw["eventDate"], frozenset(t["abbreviation"].upper() for t in teams if t.get("abbreviation"))


def moneyline(raw: dict) -> dict | None:
    return next((m for m in raw.get("markets", []) if m.get("sportsMarketType") == "moneyline" and m.get("enableOrderBook")
                 and m.get("acceptingOrders") and not m.get("closed")), None)


def pair_games(kalshi: list[Event], poly_raw: list[dict]) -> list[tuple[Event, dict]]:
    """Kalshi game-winner events paired with the Polymarket event for the same date and the same two teams."""
    index: dict[tuple, list[dict]] = {}
    for raw in poly_raw:
        key = poly_game_key(raw)
        if key and len(key[1]) == 2 and moneyline(raw):
            index.setdefault(key, []).append(raw)
    out = []
    for e in kalshi:
        key = kalshi_game_key(e)
        if key is None:
            continue
        hits = index.get((key[1], key[2]), [])
        if len(hits) == 1:           # an ambiguous match (two games, same teams, same day) is skipped, never guessed
            out.append((e, hits[0]))
    return out


def team_quotes(poly_event: Event, raw: dict) -> dict[str, Quote]:
    """Polymarket moneyline as one YES quote per team code. The second team is the mirror of the first (bid <-> 1 - ask)."""
    ml = next((q for q in poly_event.quotes if q.kind == "moneyline"), None)
    if ml is None:
        return {}
    by_name = {t["name"]: t["abbreviation"].upper() for t in raw.get("teams", [])}
    first = by_name.get(ml.yes_name)
    if first is None:
        return {}
    other = next(code for code in by_name.values() if code != first)
    mirror = Quote(**{**ml.__dict__, "label": f"not {ml.yes_name}", "bid": None if ml.ask is None else 1.0 - ml.ask, "bid_size": ml.ask_size,
                      "ask": None if ml.bid is None else 1.0 - ml.bid, "ask_size": ml.bid_size, "yes_name": other})
    return {first: ml, other: mirror}
