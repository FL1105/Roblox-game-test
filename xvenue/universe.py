"""Scan every open event on Kalshi and Polymarket for same-venue arbitrage: bundles of positions that pay at least as much as they cost,
whatever happens, with no second venue or price source involved.

Kalshi
  complement   Yes ask + No ask of one market below $1 (a crossed book).
  all-no       In a mutually exclusive event at most one market resolves Yes, so buying No in all n markets pays at least n - 1.
  ladder       Strike markets of one event nest: Yes(low strike) + No(high strike) pays at least $1 (mirrored for 'less than' strikes).
Polymarket
  crossed      A market whose best bid is above its best ask.
  all-yes      A negative-risk event resolves exactly one outcome Yes, so Yes in every outcome pays $1.
  all-no       The same event: No in every outcome pays at least n - 1 (also fine when the outcome list is incomplete).

Prices are the top of the book from the bulk listings; a finding is only as good as that snapshot, and `verify_poly` re-reads the real order
books for Polymarket ones before they are believed."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass

from poly_mm.api import GAMMA, ApiError, get_json

from .arb import KALSHI_FEE_RATE, POLY_FEE_RATE

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class Leg:
    venue: str  # "kalshi" or "polymarket"
    label: str
    price: float
    fee_rate: float  # per-share fee = rate * p * (1 - p); Kalshi rounds each order's total up to the cent

    def fee(self, n: float) -> float:
        raw = self.fee_rate * n * self.price * (1 - self.price)
        return math.ceil(raw * 100 - 1e-9) / 100 if self.venue == "kalshi" else raw


@dataclass(frozen=True)
class Finding:
    venue: str
    kind: str
    event: str
    title: str
    legs: tuple[Leg, ...]
    payout: float  # guaranteed payout per unit
    max_units: float  # what the top of book supports
    note: str = ""
    risk: str = ""  # non-empty when the bundle is not guaranteed to pay (shown, and kept out of the headline count)

    @property
    def unit_cost(self) -> float:
        return sum(l.price for l in self.legs)

    def profit(self, n: float) -> float:
        return n * self.payout - n * self.unit_cost - sum(l.fee(n) for l in self.legs)

    def cost(self, n: float) -> float:
        return n * self.unit_cost + sum(l.fee(n) for l in self.legs)

    def best(self, bankroll: float = math.inf) -> tuple[float, float]:
        """(units, profit) at the most profitable size whose whole cost, fees included, fits in `bankroll`. Fees round, so several sizes are tried."""
        cap = int(min(self.max_units, bankroll / self.unit_cost if self.unit_cost else 0))
        sizes = sorted({n for n in (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000) if n <= cap} | ({cap} if cap >= 1 else set()))
        best = (0.0, 0.0)
        for n in sizes:
            if self.cost(n) <= bankroll and self.profit(n) > best[1]:
                best = (float(n), self.profit(n))
        return best


# ---- Kalshi -----------------------------------------------------------------------------------------------------------------------------

def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def kalshi_quote(m: dict) -> dict | None:
    """Top-of-book of one active Kalshi market, or None when it cannot be traded."""
    if m.get("status") not in ("active", "open"):
        return None
    ya, na = _f(m.get("yes_ask_dollars")), _f(m.get("no_ask_dollars"))
    ysz, nsz = _f(m.get("yes_ask_size_fp")), _f(m.get("yes_bid_size_fp"))  # a No ask is a Yes bid
    if not (0 < ya <= 1 and 0 < na <= 1) or ysz <= 0 or nsz <= 0:
        return None
    rules = re.sub(r"[\d,.]+", "#", m.get("rules_primary") or "")  # the resolution text with the numbers blanked out
    return {"t": m["ticker"], "ya": ya, "na": na, "ysz": ysz, "nsz": nsz, "strike_type": m.get("strike_type"),
            "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "rules": rules}


ABOVE = re.compile(r"\b(above|greater than|at least|more than|over|higher than|exceeds?)\b", re.I)
BELOW = re.compile(r"\b(below|less than|fewer than|under|lower than)\b", re.I)
EXACT = re.compile(r"\b(exactly|between|equal to)\b", re.I)


def nests(kind: str, rules: str) -> bool:
    """Whether a market's own resolution text says it is a threshold ('above 80,000') and not a bracket ('exactly 8', 'between')."""
    return not EXACT.search(rules) and bool((ABOVE if kind == "greater" else BELOW).search(rules))


def kalshi_findings(event: dict, mult: float = 1.0) -> list[Finding]:
    rate = KALSHI_FEE_RATE * mult
    qs = [q for q in (kalshi_quote(m) for m in event.get("markets") or []) if q]
    ev, title = event["event_ticker"], event.get("title", "")
    out: list[Finding] = []
    for q in qs:  # complement
        if q["ya"] + q["na"] < 1.0:
            out.append(Finding("kalshi", "complement", ev, title, (Leg("kalshi", f"Yes {q['t']}", q["ya"], rate), Leg("kalshi", f"No {q['t']}", q["na"], rate)), 1.0, min(q["ysz"], q["nsz"])))
    n_all = len(event.get("markets") or [])
    if event.get("mutually_exclusive") and len(qs) == n_all and n_all >= 2:  # every market must be tradable, or the bundle is incomplete
        legs = tuple(Leg("kalshi", f"No {q['t']}", q["na"], rate) for q in qs)
        out.append(Finding("kalshi", "all-no", ev, title, legs, n_all - 1.0, min(q["nsz"] for q in qs)))
    for kind, key, hi_is_yes in (("greater", "floor", False), ("less", "cap", True)):
        ladder = sorted((q for q in qs if q["strike_type"] == kind and q[key] is not None and nests(kind, q["rules"])), key=lambda q: _f(q[key]))
        for i, a in enumerate(ladder):
            for b in ladder[i + 1:]:
                if a["rules"] != b["rules"]:  # not the same quantity (a spread event lists both teams' lines): the markets do not nest
                    continue
                lo, hi = (a, b)
                yes, no = (hi, lo) if hi_is_yes else (lo, hi)  # greater: Yes(low)+No(high); less: Yes(high)+No(low)
                if yes["ya"] + no["na"] < 1.0:
                    out.append(Finding("kalshi", "ladder", ev, title, (Leg("kalshi", f"Yes {yes['t']}", yes["ya"], rate), Leg("kalshi", f"No {no['t']}", no["na"], rate)), 1.0, min(yes["ysz"], no["nsz"])))
    return out


# ---- Polymarket ---------------------------------------------------------------------------------------------------------------------------

def poly_rate(m: dict) -> float:
    if not m.get("feesEnabled"):
        return 0.0
    fs = m.get("feeSchedule") or {}
    return float(fs.get("rate", POLY_FEE_RATE))


def poly_findings(event: dict) -> list[Finding]:
    ev, title = event.get("slug", ""), event.get("title", "")
    ms = [m for m in event.get("markets") or [] if m.get("active") and not m.get("closed") and m.get("enableOrderBook", True) and m.get("acceptingOrders", True)]
    out: list[Finding] = []
    for m in ms:  # crossed book on one market
        bb, ba = m.get("bestBid"), m.get("bestAsk")
        if bb is not None and ba is not None and 0 < ba < bb < 1:
            r = poly_rate(m)
            out.append(Finding("polymarket", "crossed", ev, title, (Leg("polymarket", f"Yes {m.get('slug')}", ba, r), Leg("polymarket", f"No {m.get('slug')}", 1 - bb, r)), 1.0, 0.0, "size unknown: verify"))
    if event.get("negRisk") and len(ms) == len(event.get("markets") or []) and len(ms) >= 2 and all(m.get("bestAsk") and m.get("bestBid") is not None for m in ms):
        asks = [(m, float(m["bestAsk"])) for m in ms]
        out_all_yes = Finding("polymarket", "all-yes", ev, title, tuple(Leg("polymarket", f"Yes {m.get('slug')}", p, poly_rate(m)) for m, p in asks), 1.0, 0.0,
                              "size unknown: verify", "pays nothing if the winner is not one of the listed outcomes")
        if out_all_yes.unit_cost < 1.0:
            out.append(out_all_yes)
        no_asks = [(m, 1.0 - float(m["bestBid"])) for m in ms]
        all_no = Finding("polymarket", "all-no", ev, title, tuple(Leg("polymarket", f"No {m.get('slug')}", p, poly_rate(m)) for m, p in no_asks), len(ms) - 1.0, 0.0, "size unknown: verify")
        if all_no.unit_cost < all_no.payout:
            out.append(all_no)
    return out


def verify_poly(f: Finding, event: dict, depth_floor: float = 5.0) -> Finding | None:
    """Re-price a Polymarket finding from the real order books (ask depth per leg). None if it no longer works."""
    by_slug = {m.get("slug"): m for m in event.get("markets") or []}
    legs, size = [], 1e12
    for leg in f.legs:
        side, slug = leg.label.split(" ", 1)
        m = by_slug.get(slug)
        if not m:
            return None
        try:
            tokens = json.loads(m["clobTokenIds"])
            book = get_json("https://clob.polymarket.com/book", {"token_id": tokens[0 if side == "Yes" else 1]})
        except (ApiError, KeyError, ValueError):
            return None
        asks = sorted(((float(a["price"]), float(a["size"])) for a in book.get("asks", [])), key=lambda x: x[0])
        if not asks:
            return None
        legs.append(Leg("polymarket", leg.label, asks[0][0], leg.fee_rate))
        size = min(size, asks[0][1])
    out = Finding(f.venue, f.kind, f.event, f.title, tuple(legs), f.payout, size, "verified against the order books", f.risk)
    return out if size >= depth_floor and out.profit(min(size, 100)) > 0 else None


# ---- fetching ---------------------------------------------------------------------------------------------------------------------------

def fetch_kalshi_events(max_pages: int = 120) -> list[dict]:
    events, cur = [], None
    for _ in range(max_pages):
        p = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cur:
            p["cursor"] = cur
        d = get_json(f"{KALSHI}/events", p)
        events += d.get("events", [])
        cur = d.get("cursor")
        if not cur:
            break
        time.sleep(0.3)  # Kalshi rate-limits bulk reads
    return events


def fetch_kalshi_fee_multipliers() -> dict[str, float]:
    rows = get_json(f"{KALSHI}/series", {"limit": 2000}).get("series", [])
    return {s["ticker"]: float(s.get("fee_multiplier", 1)) for s in rows}


def fetch_poly_events(limit_events: int = 2500) -> list[dict]:
    out, off = [], 0
    while off < limit_events:
        try:
            batch = get_json(f"{GAMMA}/events", {"active": "true", "closed": "false", "limit": 500, "offset": off})
        except ApiError:
            break  # the API refuses deep offsets
        if not batch:
            break
        out += batch
        off += 500
    return out
