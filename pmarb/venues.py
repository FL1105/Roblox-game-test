"""Fetch and normalise live top-of-book for every open market on Kalshi and Polymarket (public endpoints, throttled)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from kalshi_btc15 import api

from .model import Fee, Quote

KALSHI = api.BASE
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI_BASE_RATE = 0.07
_poly_gate = api.new_gate(0.08)


@dataclass(frozen=True)
class Event:
    venue: str
    id: str
    title: str
    exclusive: bool                  # at most one market can resolve YES
    quotes: tuple[Quote, ...]
    close_ts: int | None = None
    extra: dict = field(default_factory=dict)


def price(raw) -> float | None:
    try:
        return None if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        return None


def kalshi_fees() -> dict[str, Fee]:
    """Fee per series ticker from the one bulk /series call (fee_multiplier scales the 0.07 schedule)."""
    out = {}
    for s in api.get_json(f"{KALSHI}/series").get("series", []):
        out[s["ticker"]] = Fee(KALSHI_BASE_RATE * float(s.get("fee_multiplier") or 0), 1.0, cent_round=True)
    return out


def kalshi_quote(event: dict, m: dict, fee: Fee) -> Quote:
    bid, ask = price(m.get("yes_bid_dollars")), price(m.get("yes_ask_dollars"))
    stype = m.get("strike_type") or ""
    floor, cap = m.get("floor_strike"), m.get("cap_strike")
    # A threshold has exactly one bound. Exact-count bins arrive as "less" with floor == cap (or both set): not a ladder.
    strike = floor if stype in ("greater", "greater_or_equal") and cap is None else cap if stype in ("less", "less_or_equal") and floor is None else None
    return Quote(
        "kalshi", event["event_ticker"], m["ticker"], event.get("title", ""), m.get("yes_sub_title") or m.get("title", ""),
        bid if bid and bid > 0 else None, price(m.get("yes_bid_size_fp")) or 0.0,
        ask if ask and ask < 1 else None, price(m.get("yes_ask_size_fp")) or 0.0, fee,
        close_ts=api.iso_ts(m["close_time"]) if m.get("close_time") else None,
        strike_type=stype, strike=price(strike), rules=m.get("rules_primary", ""))


def kalshi_event(e: dict, fees: dict[str, Fee]) -> Event | None:
    fee = fees.get(e.get("series_ticker", ""), Fee(KALSHI_BASE_RATE, 1.0, cent_round=True))
    quotes = tuple(kalshi_quote(e, m, fee) for m in e.get("markets", []) if m.get("status") == "active")
    if not quotes:
        return None
    return Event("kalshi", e["event_ticker"], e.get("title", ""), bool(e.get("mutually_exclusive")), quotes,
                 extra={"category": e.get("category"), "series": e.get("series_ticker")})


def kalshi_snapshot(progress=None) -> list[Event]:
    fees = kalshi_fees()
    events, cursor, pages = [], "", 0
    while True:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = api.get_json(f"{KALSHI}/events", params)
        pages += 1
        events += [ev for e in page.get("events", []) if (ev := kalshi_event(e, fees)) is not None]
        cursor = page.get("cursor") or ""
        if progress and pages % 20 == 0:
            progress(f"kalshi pages {pages}, events {len(events)}")
        if not cursor:
            return events


def kalshi_refresh(event_tickers: list[str], fees: dict[str, Fee]) -> dict[str, Event]:
    """Fresh quotes for specific events (one call each)."""
    out = {}
    for t in event_tickers:
        try:
            d = api.get_json(f"{KALSHI}/events/{t}", {"with_nested_markets": "true"})
        except api.ApiError:
            continue
        e = dict(d["event"]); e["markets"] = d.get("markets") or e.get("markets", [])
        if (ev := kalshi_event(e, fees)) is not None:
            out[t] = ev
    return out


def poly_fee(m: dict) -> Fee:
    sched = m.get("feeSchedule") or {}
    return Fee(float(sched.get("rate") or 0), float(sched.get("exponent") or 1), cent_round=False)


def best(levels: list[dict], highest: bool) -> tuple[float | None, float]:
    rows = [(float(l["price"]), float(l["size"])) for l in levels or [] if float(l.get("size", 0)) > 0]
    if not rows:
        return None, 0.0
    p = max(r[0] for r in rows) if highest else min(r[0] for r in rows)
    return p, sum(s for pr, s in rows if pr == p)


def poly_events(progress=None, max_pages: int = 600) -> list[dict]:
    """All active events via keyset paging (offset paging stops working past 2,000)."""
    out, cursor = [], ""
    for page_no in range(1, max_pages + 1):
        params = {"active": "true", "closed": "false", "limit": 100}
        if cursor:
            params["after_cursor"] = cursor
        page = api.get_json(f"{GAMMA}/events/keyset", params, gate=_poly_gate)
        out += page.get("events", [])
        cursor = page.get("next_cursor") or ""
        if progress and page_no % 20 == 0:
            progress(f"polymarket events {len(out)}")
        if not cursor:
            break
    return out


def poly_books(tokens: list[str]) -> dict[str, dict]:
    books = {}
    for i in range(0, len(tokens), 500):
        for b in api.get_json(f"{CLOB}/books", gate=_poly_gate, body=[{"token_id": t} for t in tokens[i:i + 500]]):
            books[b["asset_id"]] = b
    return books


def poly_snapshot(progress=None, raw_events: list[dict] | None = None, market_filter=None) -> list[Event]:
    """Books for every open market (or just those market_filter(event, market) accepts), grouped by event."""
    raw = raw_events if raw_events is not None else poly_events(progress)
    wanted = []
    for e in raw:
        for m in e.get("markets", []):
            if market_filter is not None and not market_filter(e, m):
                continue
            if m.get("enableOrderBook") and m.get("acceptingOrders") and not m.get("closed") and m.get("clobTokenIds"):
                wanted.append((e, m, json.loads(m["clobTokenIds"])[0]))
    books = poly_books([t for _, _, t in wanted])
    by_event: dict[str, list[Quote]] = {}
    meta: dict[str, dict] = {}
    for e, m, token in wanted:
        b = books.get(token)
        if b is None:
            continue
        bid, bid_size = best(b.get("bids"), True)
        ask, ask_size = best(b.get("asks"), False)
        slug = e["slug"]
        by_event.setdefault(slug, []).append(Quote(
            "poly", slug, m.get("conditionId") or m["id"], e.get("title", ""), m.get("groupItemTitle") or m.get("question", ""),
            bid, bid_size, ask, ask_size, poly_fee(m), min_size=float(m.get("orderMinSize") or b.get("min_order_size") or 1),
            close_ts=api.iso_ts(m["endDate"]) if m.get("endDate") else None, rules=(m.get("description") or "")[:600],
            yes_name=(json.loads(m["outcomes"])[0] if m.get("outcomes") else ""), kind=m.get("sportsMarketType") or ""))
        meta[slug] = {"title": e.get("title", ""), "neg": bool(e.get("negRisk")), "all_neg": meta.get(slug, {}).get("all_neg", True) and bool(m.get("negRisk")),
                      "extra": {"teams": e.get("teams") or [], "date": e.get("eventDate"), "start": e.get("startTime"), "league": e.get("seriesSlug")}}
    return [Event("poly", slug, meta[slug]["title"], meta[slug]["neg"] and meta[slug]["all_neg"], tuple(qs), extra=meta[slug]["extra"])
            for slug, qs in by_event.items()]


def poly_sports_events(progress=None, max_pages: int = 300) -> list[dict]:
    """Active events tagged 'sports' (keyset paging)."""
    out, cursor = [], ""
    for page_no in range(1, max_pages + 1):
        params = {"active": "true", "closed": "false", "limit": 100, "tag_slug": "sports"}
        if cursor:
            params["after_cursor"] = cursor
        page = api.get_json(f"{GAMMA}/events/keyset", params, gate=_poly_gate)
        out += page.get("events", [])
        cursor = page.get("next_cursor") or ""
        if progress and page_no % 20 == 0:
            progress(f"polymarket sports events {len(out)}")
        if not cursor:
            break
    return out
