"""Live market data from Polymarket (hourly BTC Up/Down) and Kalshi (hourly BTC ladder). Public, read-only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from poly_mm.api import CLOB, GAMMA, ApiError, get_json

from .arb import Level

ET = ZoneInfo("America/New_York")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def hour_start(now: datetime | None = None) -> datetime:
    """Start of the current hour in Eastern time."""
    return (now or datetime.now(timezone.utc)).astimezone(ET).replace(minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class PolyHour:
    slug: str
    start: datetime
    strike: float | None  # the 'price to beat' (Binance hourly open): published only after the hour ends
    up_token: str
    down_token: str


def poly_slug(start_et: datetime) -> str:
    h12 = start_et.hour % 12 or 12
    return f"bitcoin-up-or-down-{start_et.strftime('%B').lower()}-{start_et.day}-{start_et.year}-{h12}{'am' if start_et.hour < 12 else 'pm'}-et"


def load_poly_hour(start_et: datetime) -> PolyHour | None:
    try:
        ev = get_json(f"{GAMMA}/events", {"slug": poly_slug(start_et)})
    except ApiError:
        return None
    if not ev:
        return None
    strike = (ev[0].get("eventMetadata") or {}).get("priceToBeat")
    try:
        up, down = json.loads(ev[0]["markets"][0]["clobTokenIds"])
    except (KeyError, ValueError):
        return None
    return PolyHour(ev[0]["slug"], start_et, float(strike) if strike else None, up, down)


def poly_asks(token: str) -> list[Level]:
    book = get_json(f"{CLOB}/book", {"token_id": token})
    return sorted(((float(a["price"]), float(a["size"])) for a in book.get("asks", [])), key=lambda x: x[0])


@dataclass(frozen=True)
class KalshiStrike:
    ticker: str
    strike: float  # 'above 72999.99' is treated as 73000


def kalshi_event(close_et: datetime) -> str:
    return f"KXBTCD-{close_et.strftime('%y')}{close_et.strftime('%b').upper()}{close_et.strftime('%d')}{close_et.strftime('%H')}"


def load_kalshi_strikes(close_et: datetime) -> list[KalshiStrike]:
    rows = get_json(f"{KALSHI}/markets", {"event_ticker": kalshi_event(close_et), "limit": 200}).get("markets", [])
    return sorted((KalshiStrike(r["ticker"], round(float(r["floor_strike"]) + 0.01, 2)) for r in rows if r.get("strike_type") == "greater" and r.get("floor_strike")), key=lambda k: k.strike)


def kalshi_asks(ticker: str) -> tuple[list[Level], list[Level]]:
    """(Yes asks, No asks). The book lists bids on each side; a bid for No at q is an offer of Yes at 1 - q."""
    book = get_json(f"{KALSHI}/markets/{ticker}/orderbook").get("orderbook_fp", {})
    yes_bids = [(float(p), float(s)) for p, s in book.get("yes_dollars", [])]
    no_bids = [(float(p), float(s)) for p, s in book.get("no_dollars", [])]
    yes_asks = sorted(((round(1 - p, 4), s) for p, s in no_bids), key=lambda x: x[0])
    no_asks = sorted(((round(1 - p, 4), s) for p, s in yes_bids), key=lambda x: x[0])
    return yes_asks, no_asks


def next_hour(start_et: datetime) -> datetime:
    return (start_et.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(ET)
