"""The same BTC 15-minute window on Kalshi, Polymarket and Limitless: quotes, pairwise hedges after fees, and a log.

The three settle on different price sources and start references (Kalshi: 60 s BRTI average; Polymarket: Chainlink TWAP;
Limitless: a Chainlink / Pyth feed), so a gap is partly the market pricing that difference and a hedge can pay $0.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations

from kalshi_btc15 import api

from . import venues
from .model import Fee, Quote

LIMITLESS = "https://api.limitless.exchange"
_lim_gate = api.new_gate(0.15)
# Limitless CLOB taker BUY fee (percent of the contracts bought) by price, from its fee page. Legacy-venue curve; markets on
# "new trading venues" charge a USDC fee on a curve that is not published, so this is an approximation.
LIMITLESS_BUY = [(0.0, 3.00), (0.50, 3.00), (0.55, 2.52), (0.60, 2.13), (0.65, 1.80), (0.70, 1.51), (0.75, 1.26), (0.80, 1.05),
                 (0.85, 0.85), (0.90, 0.68), (0.95, 0.53), (0.99, 0.42), (1.0, 0.40)]


def limitless_buy_rate(price: float) -> float:
    for (p0, f0), (p1, f1) in zip(LIMITLESS_BUY, LIMITLESS_BUY[1:]):
        if p0 <= price <= p1:
            return (f0 + (f1 - f0) * (price - p0) / (p1 - p0)) / 100.0 if p1 > p0 else f0 / 100.0
    return 0.03


def buy_cost(venue: str, price: float, fee: Fee | None = None) -> float:
    """Dollars paid per contract that ends up in your hands, fee included (Limitless takes its fee in tokens)."""
    if venue == "limitless":
        return price / (1.0 - limitless_buy_rate(price))
    return price + (fee.total(price, 1000) / 1000.0 if fee else 0.0)


def limitless_quote(slug: str) -> Quote | None:
    try:
        ob = api.get_json(f"{LIMITLESS}/markets/{slug}/orderbook", gate=_lim_gate)
    except api.ApiError:
        return None
    bids = [(float(b["price"]), b["size"] / 1e6) for b in ob.get("bids", []) if b["size"] > 0]
    asks = [(float(a["price"]), a["size"] / 1e6) for a in ob.get("asks", []) if a["size"] > 0]
    bid = max(bids, default=(None, 0.0)); ask = min(asks, default=(None, 0.0))
    return Quote("limitless", slug, slug, slug, "Up", bid[0], bid[1], ask[0], ask[1], Fee(0.0), min_size=50.0)


def poll_serial() -> list[dict]:
    """One row per open window with each venue's Up bid/ask and the best pairwise hedge net of fees (per set, taker both legs)."""
    fees = venues.kalshi_fees()
    kfee = fees.get("KXBTC15M", Fee(venues.KALSHI_BASE_RATE, 1.0, cent_round=True))
    now = int(time.time())
    rows = []
    for m in api.get_json(f"{venues.KALSHI}/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 6}).get("markets", []):
        close = api.iso_ts(m["close_time"]); start = close - 900
        quotes: dict[str, tuple[Quote, Fee | None]] = {}
        kq = venues.kalshi_quote({"event_ticker": m.get("event_ticker", ""), "title": m.get("title", "")}, m, kfee)
        quotes["kalshi"] = (kq, kfee)
        try:
            ev = api.get_json(f"{venues.GAMMA}/events", {"slug": f"btc-updown-15m-{start}"}, gate=venues._poly_gate)
            pm = ev[0]["markets"][0]
            outs, toks = json.loads(pm["outcomes"]), json.loads(pm["clobTokenIds"])
            book = api.get_json(f"{venues.CLOB}/book", {"token_id": toks[outs.index("Up")]}, gate=venues._poly_gate)
            bid, bs = venues.best(book.get("bids"), True); ask, asz = venues.best(book.get("asks"), False)
            pf = venues.poly_fee(pm)
            quotes["poly"] = (Quote("poly", ev[0]["slug"], "p", "", "Up", bid, bs, ask, asz, pf, min_size=5.0), pf)
        except (api.ApiError, IndexError, KeyError, ValueError):
            pass
        lq = limitless_quote(f"btc-up-or-down-15-min-{start}")
        if lq is not None:
            quotes["limitless"] = (lq, None)
        row = {"t": now, "close": close, "left_s": close - now, "venues": {v: [q.bid, q.ask] for v, (q, _) in quotes.items()}, "best": None}
        best = None
        for a, b in permutations(quotes, 2):                       # YES on a at its ask, NO on b at 1 - its bid
            (qa, fa), (qb, fb) = quotes[a], quotes[b]
            if qa.ask is None or qb.bid is None:
                continue
            cost = buy_cost(a, qa.ask, fa) + buy_cost(b, 1.0 - qb.bid, fb)
            gross = 1.0 - (qa.ask + 1.0 - qb.bid)
            net = 1.0 - cost
            if best is None or net > best["net"]:
                best = {"yes_on": a, "no_on": b, "gross": gross, "net": net}
        row["best"] = best
        rows.append(row)
    return rows


_poly_window: dict[int, tuple[str, str, Fee]] = {}
_pool = ThreadPoolExecutor(3)


def poly_window(start: int):
    """(slug, Up token, fee) for one window, looked up once."""
    if start not in _poly_window:
        ev = api.get_json(f"{venues.GAMMA}/events", {"slug": f"btc-updown-15m-{start}"}, gate=venues._poly_gate)[0]
        pm = ev["markets"][0]
        outs, toks = json.loads(pm["outcomes"]), json.loads(pm["clobTokenIds"])
        _poly_window[start] = (ev["slug"], toks[outs.index("Up")], venues.poly_fee(pm))
    return _poly_window[start]


def _kalshi_now(close: int):
    d = api.get_json(f"{venues.KALSHI}/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 6})
    t = time.time()
    for m in d.get("markets", []):
        if api.iso_ts(m["close_time"]) == close:
            return t, m
    return t, None


def _poly_now(token: str):
    b = api.get_json(f"{venues.CLOB}/book", {"token_id": token}, gate=venues._poly_gate)
    return time.time(), b


def _lim_now(slug: str):
    ob = api.get_json(f"{LIMITLESS}/markets/{slug}/orderbook", gate=_lim_gate)
    return time.time(), ob


def hedge_total(a: str, qa: dict, b: str, qb: dict, fa: Fee | None, fb: Fee | None, fill: float = 1.0) -> dict | None:
    """YES on venue a at its ask + NO on venue b at 1 - its bid, sized to `fill` x the thinner side and each venue's minimum order.

    Returns sets, gross and net dollars (taker fees on both legs; Limitless takes its fee in tokens)."""
    if qa["ask"] is None or qb["bid"] is None:
        return None
    qty = math.floor(fill * min(qa["ask_size"], qb["bid_size"]))
    if qty < max(qa.get("min", 1), qb.get("min", 1)):
        return None
    pa, pb = qa["ask"], 1.0 - qb["bid"]
    def leg_cost(v, price, fee):
        if v == "limitless":
            return qty * price / (1.0 - limitless_buy_rate(price))
        return qty * price + (fee.total(price, qty) if fee else 0.0)
    cost = leg_cost(a, pa, fa) + leg_cost(b, pb, fb)
    return {"yes_on": a, "no_on": b, "sets": qty, "gross": qty * (1.0 - pa - pb), "net": qty - cost}


_fees: dict[str, Fee] = {}


def poll_sync() -> list[dict]:
    """One poll with the three venues fetched at the same moment; logs sizes and when each answer arrived."""
    if not _fees:
        _fees.update(venues.kalshi_fees())
    kfee = _fees.get("KXBTC15M", Fee(venues.KALSHI_BASE_RATE, 1.0, cent_round=True))
    now = int(time.time()); close = (now // 900 + 1) * 900; start = close - 900
    try:
        slug, token, pfee = poly_window(start)
    except (api.ApiError, IndexError, KeyError, ValueError):
        return []
    futs = {"kalshi": _pool.submit(_kalshi_now, close), "poly": _pool.submit(_poly_now, token),
            "limitless": _pool.submit(_lim_now, f"btc-up-or-down-15-min-{start}")}
    out, stamps = {}, {}
    for v, f in futs.items():
        try:
            stamps[v], raw = f.result()
        except api.ApiError:
            continue
        if v == "kalshi":
            if raw is None:
                continue
            bid, ask = venues.price(raw.get("yes_bid_dollars")), venues.price(raw.get("yes_ask_dollars"))
            out[v] = {"bid": bid if bid and bid > 0 else None, "bid_size": venues.price(raw.get("yes_bid_size_fp")) or 0.0,
                      "ask": ask if ask and ask < 1 else None, "ask_size": venues.price(raw.get("yes_ask_size_fp")) or 0.0, "min": 1}
        elif v == "poly":
            bid, bs = venues.best(raw.get("bids"), True); ask, asz = venues.best(raw.get("asks"), False)
            out[v] = {"bid": bid, "bid_size": bs, "ask": ask, "ask_size": asz, "min": 5}
        else:
            bids = [(float(b["price"]), b["size"] / 1e6) for b in raw.get("bids", []) if b["size"] > 0]
            asks = [(float(a["price"]), a["size"] / 1e6) for a in raw.get("asks", []) if a["size"] > 0]
            bb = max(bids, default=(None, 0.0)); ba = min(asks, default=(None, 0.0))
            out[v] = {"bid": bb[0], "bid_size": bb[1], "ask": ba[0], "ask_size": ba[1], "min": 50}
    fee_of = {"kalshi": kfee, "poly": pfee, "limitless": None}
    best = None
    for a, b in permutations(out, 2):
        h = hedge_total(a, out[a], b, out[b], fee_of[a], fee_of[b])
        if h and (best is None or h["net"] > best["net"]):
            best = h
    skew = (max(stamps.values()) - min(stamps.values())) if len(stamps) > 1 else None
    return [{"t": now, "close": close, "left_s": close - now, "quotes": out, "skew_s": skew, "best": best}]


def watch(minutes: float, interval: float, log_path: str, say=print) -> None:
    end, n = time.time() + minutes * 60, 0
    with open(log_path, "a") as log:
        while time.time() < end:
            t0 = time.time()
            try:
                rows = poll_sync()
            except api.ApiError as e:
                say(f"poll failed: {e}")
                rows = []
            n += 1
            for r in rows:
                log.write(json.dumps(r) + "\n")
            log.flush()
            b = rows[0]["best"] if rows else None
            say(f"poll {n}: " + (f"skew {rows[0]['skew_s']:.2f}s" + (f", best {b['sets']} sets gross ${b['gross']:+.2f} net ${b['net']:+.2f} (YES {b['yes_on']}, NO {b['no_on']})" if b else ", no hedge") if rows else "no window"))
            time.sleep(max(0.0, interval - (time.time() - t0)))
