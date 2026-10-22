"""The same BTC 15-minute window on Kalshi, Polymarket and Limitless: quotes, pairwise hedges after fees, and a log.

The three settle on different price sources and start references (Kalshi: 60 s BRTI average; Polymarket: Chainlink TWAP;
Limitless: a Chainlink / Pyth feed), so a gap is partly the market pricing that difference and a hedge can pay $0.
"""

from __future__ import annotations

import json
import time
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


def poll() -> list[dict]:
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


def watch(minutes: float, interval: float, log_path: str, say=print) -> None:
    end, n = time.time() + minutes * 60, 0
    with open(log_path, "a") as log:
        while time.time() < end:
            t0 = time.time()
            try:
                rows = poll()
            except api.ApiError as e:
                say(f"poll failed: {e}")
                rows = []
            n += 1
            for r in rows:
                log.write(json.dumps(r) + "\n")
            log.flush()
            top = max((r["best"] for r in rows if r["best"]), key=lambda b: b["net"], default=None)
            say(f"poll {n}: {len(rows)} windows" + (f", best gross {top['gross']:+.3f} net {top['net']:+.3f} (YES {top['yes_on']}, NO {top['no_on']})" if top else ""))
            time.sleep(max(0.0, interval - (time.time() - t0)))
