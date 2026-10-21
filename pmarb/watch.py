"""Poll matched markets on both venues repeatedly and log every cross-venue gap (read-only; no orders)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from kalshi_btc15 import api

from . import match, venues
from .detect import cross_pair
from .model import Fee, Quote


@dataclass(frozen=True)
class Pair:
    label: str                       # human name, e.g. "KXNBAGAME-26OCT21SACLAC [SAC]"
    kind: str                        # "game" | "btc15"
    kalshi_ticker: str
    kalshi_fee: Fee
    poly_token: str                  # the token whose price is the same proposition as Kalshi's YES
    poly_flip: bool                  # True when the Kalshi YES equals the Poly token's complement
    poly_fee: Fee
    poly_min: float
    poly_event: str = ""


def game_pairs(pairs: list[tuple[venues.Event, dict]], fees: dict[str, Fee]) -> list[Pair]:
    out = []
    for k, raw in pairs:
        ml = match.moneyline(raw)
        if ml is None:
            continue
        outcomes, tokens = json.loads(ml["outcomes"]), json.loads(ml["clobTokenIds"])
        by_name = {t["name"]: t["abbreviation"].upper() for t in raw["teams"]}
        if len(outcomes) != 2 or any(o not in by_name for o in outcomes):
            continue
        code0 = by_name[outcomes[0]]
        for q in k.quotes:
            code = q.market.rsplit("-", 1)[-1].upper()
            if code in by_name.values():
                out.append(Pair(f"{k.id} [{code}]", "game", q.market, q.fee, tokens[0], code != code0, venues.poly_fee(ml),
                                float(ml.get("orderMinSize") or 5), raw["slug"]))
    return out


def btc15_pairs(fees: dict[str, Fee]) -> list[Pair]:
    """The open KXBTC15M windows that also exist on Polymarket (same window, same direction)."""
    out = []
    fee = fees.get("KXBTC15M", Fee(venues.KALSHI_BASE_RATE, 1.0, cent_round=True))
    for m in api.get_json(f"{venues.KALSHI}/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 20}).get("markets", []):
        close = api.iso_ts(m["close_time"])
        try:
            ev = api.get_json(f"{venues.GAMMA}/events", {"slug": f"btc-updown-15m-{close - 900}"}, gate=venues._poly_gate)
        except api.ApiError:
            continue
        if not ev or not ev[0].get("markets"):
            continue
        pm = ev[0]["markets"][0]
        outcomes = json.loads(pm["outcomes"])
        if "Up" not in outcomes or not pm.get("acceptingOrders"):
            continue
        out.append(Pair(f"{m['ticker']} [Up]", "btc15", m["ticker"], fee, json.loads(pm["clobTokenIds"])[outcomes.index("Up")], False,
                        venues.poly_fee(pm), float(pm.get("orderMinSize") or 5), ev[0]["slug"]))
    return out


def kalshi_quotes(tickers: list[str], fees: dict[str, Fee]) -> dict[str, Quote]:
    out = {}
    for i in range(0, len(tickers), 100):
        for m in api.get_json(f"{venues.KALSHI}/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 100}).get("markets", []):
            fee = fees.get(m["ticker"].split("-")[0], Fee(venues.KALSHI_BASE_RATE, 1.0, cent_round=True))
            out[m["ticker"]] = venues.kalshi_quote({"event_ticker": m.get("event_ticker", ""), "title": m.get("title", "")}, m, fee)
    return out


def poly_quote(p: Pair, book: dict) -> Quote:
    bid, bid_size = venues.best(book.get("bids"), True)
    ask, ask_size = venues.best(book.get("asks"), False)
    if p.poly_flip:
        bid, bid_size, ask, ask_size = (None if ask is None else 1.0 - ask), ask_size, (None if bid is None else 1.0 - bid), bid_size
    return Quote("poly", p.poly_event, p.poly_token, p.label, p.label, bid, bid_size, ask, ask_size, p.poly_fee, min_size=p.poly_min)


def cycle(pairs: list[Pair], fees: dict[str, Fee]) -> list[dict]:
    """One poll: a row per pair with both books, the best taker hedge after fees, and the gross gaps."""
    kq = kalshi_quotes([p.kalshi_ticker for p in pairs], fees)
    books = venues.poly_books(sorted({p.poly_token for p in pairs}))
    now = int(time.time())
    rows = []
    for p in pairs:
        k, b = kq.get(p.kalshi_ticker), books.get(p.poly_token)
        if k is None or b is None:
            continue
        q = poly_quote(p, b)
        opps = cross_pair("cross", p.label, k, q, same_resolution=False, note=p.kind)
        best = max(opps, key=lambda o: o.net) if opps else None
        # gross gaps before any fee: buy YES on one venue at its ask and NO on the other at 1 - bid
        gross = [1.0 - (k.ask + (1.0 - q.bid))] if k.ask is not None and q.bid is not None else []
        gross += [1.0 - (q.ask + (1.0 - k.bid))] if q.ask is not None and k.bid is not None else []
        maker = [x for x in ((q.bid - k.bid) if q.bid is not None and k.bid is not None else None,
                             (k.ask - q.ask) if k.ask is not None and q.ask is not None else None) if x is not None]
        rows.append({"t": now, "label": p.label, "kind": p.kind, "k_bid": k.bid, "k_ask": k.ask, "p_bid": q.bid, "p_ask": q.ask,
                     "gross": max(gross) if gross else None, "maker_edge": max(maker) if maker else None,
                     "net": best.net if best else None, "qty": best.qty if best else 0, "fees": best.fees if best else None})
    return rows


def watch(pairs: list[Pair], fees: dict[str, Fee], minutes: float, interval: float, log_path: str, say=print,
          with_btc15: bool = True) -> list[dict]:
    """Poll `pairs` every `interval` seconds for `minutes`, adding the currently open BTC 15-minute windows each cycle."""
    end, rows = time.time() + minutes * 60, []
    fixed = pairs
    with open(log_path, "a") as log:
        n = 0
        while time.time() < end:
            t0 = time.time()
            try:
                batch = cycle(fixed + (btc15_pairs(fees) if with_btc15 else []), fees)
            except api.ApiError as e:
                say(f"cycle failed: {e}")
                batch = []
            n += 1
            for r in batch:
                log.write(json.dumps(r) + "\n")
            log.flush()
            rows += batch
            pos = [r for r in batch if r["net"] is not None and r["net"] > 0]
            say(f"cycle {n}: {len(batch)} pairs, net>0 after fees: {len(pos)}, best gross {max([r['gross'] for r in batch if r['gross'] is not None], default=float('nan')):+.3f}")
            time.sleep(max(0.0, interval - (time.time() - t0)))
    return rows
