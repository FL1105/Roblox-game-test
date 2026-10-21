"""Usage:
    python -m xvenue scan --minutes 30      # watch the current hour on both venues; log every snapshot to data/xvenue/scan.jsonl

Read-only: public market data only, no account, no keys, no order code."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from poly_mm.api import get_json
from .arb import Hedge, best_hedge
from .ladder import ladder_arbs, poly_set
from poly_mm.btc import fetch_range
from .venues import KALSHI, hour_start, kalshi_asks, kalshi_event, load_kalshi_strikes, load_poly_hour, next_hour, poly_asks

LOG = Path("data/xvenue/scan.jsonl")
NEAR = 1500.0  # only strikes within this many dollars of Polymarket's strike are worth looking at


def snapshot(poly, strikes, strike: float, margin: float) -> tuple[list[Hedge], float]:
    """`strike` is Polymarket's strike translated into Kalshi's price terms; Kalshi strikes closer than `margin` to it are skipped, because
    there the hedge direction (and the guarantee) depends on source differences we cannot see."""
    up, down = poly_asks(poly.up_token), poly_asks(poly.down_token)
    near = [k for k in strikes if margin <= abs(k.strike - strike) <= NEAR]
    out: list[Hedge] = []
    for k in near:
        yes_asks, no_asks = kalshi_asks(k.ticker)
        if k.strike >= strike:
            h = best_hedge(strike, k.strike, up, no_asks)
        else:
            h = best_hedge(strike, k.strike, down, yes_asks)
        if h:
            out.append(h)
    return out, (up[0][0] + down[0][0]) if up and down else float("nan")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xvenue", description="Polymarket vs Kalshi hourly BTC hedge scanner (read-only)")
    p.add_argument("cmd", choices=("scan",))
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--every", type=float, default=5.0, help="seconds between snapshots")
    p.add_argument("--margin", type=float, default=60.0, help="skip Kalshi strikes closer than this many dollars to Polymarket's strike in Kalshi terms (default 60: the Binance-vs-Coinbase basis has sd $14)")
    args = p.parse_args(argv)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + args.minutes * 60
    n = naive = allin = ladder_hits = set_hits = 0
    best_all: Hedge | None = None
    hour, poly, strikes = None, None, []
    print(f"{'time ET':8s} {'strike*':>11s} {'nearest':>8s} {'gap$':>6s} {'sides':9s} {'naive sum':>9s} {'all-in/share':>12s} {'profit/share':>12s} {'shares':>7s}")
    with LOG.open("a") as log:
        while time.time() < end:
            try:
                h = hour_start()
                if h != hour or not poly:
                    hour, poly = h, load_poly_hour(h)
                    strikes = load_kalshi_strikes(next_hour(h)) if poly else []
                    if poly:
                        # Polymarket's strike is a Binance BTC/USDT price, about $97 above the USD price Kalshi's index follows: use the USD open
                        t0 = int(h.timestamp())
                        eff = fetch_range(t0 - 60, t0 + 120).get(t0)
                        print(f"-- hour {h:%a %H:%M} ET: strike in Kalshi terms {eff:,.2f} (Coinbase open); kalshi {kalshi_event(next_hour(h))}: {len(strikes)} strikes")
                if not poly or not strikes:
                    time.sleep(args.every)
                    continue
                hedges, poly_pair = snapshot(poly, strikes, eff, args.margin)
            except Exception as e:  # network hiccups should not stop a long scan
                print(f"  (skipped: {type(e).__name__}: {e})")
                time.sleep(args.every)
                continue
            n += 1
            try:  # the same-venue checks: no settlement-source difference to worry about
                rows = get_json(f"{KALSHI}/markets", {"event_ticker": kalshi_event(next_hour(hour)), "limit": 200}).get("markets", [])
                ladder = ladder_arbs(rows)
                pset = poly_set(poly_asks(poly.up_token), poly_asks(poly.down_token))
            except Exception:
                ladder, pset = [], None
            ladder_hits += bool(ladder)
            set_hits += bool(pset and pset[0] > 0)
            if ladder:
                a = ladder[0]
                print(f"   !! KALSHI LADDER: Yes {a.low:,.0f} @ {a.yes_ask:.2f} + No {a.high:,.0f} @ {a.no_ask:.2f}: {a.per_contract:+.4f}/contract x {a.contracts:.0f} = {a.profit:+.2f} USD after fees")
            if pset and pset[0] > 0:
                print(f"   !! POLYMARKET SET: Up+Down at {pset[2]:.3f}/share x {pset[1]:.0f} = {pset[0]:+.2f} USD after fees")
            if hedges:
                best = max(hedges, key=lambda x: x.per_share)
                top = min(hedges, key=lambda x: x.top_sum)
                naive += top.top_sum < 1.0
                allin += best.profit > 0
                if best_all is None or best.per_share > best_all.per_share:
                    best_all = best
                log.write(json.dumps({"t": time.time(), "strike_kalshi_terms": eff, "poly_pair_ask_sum": poly_pair, "hedges": [h.__dict__ | {"profit": h.profit} for h in hedges]}) + "\n")
                log.flush()
                print(f"{time.strftime('%H:%M:%S'):8s} {eff:11,.0f} {best.strike:8,.0f} {best.gap:6.0f} {best.poly_side + '+' + best.kalshi_side:9s} {top.top_sum:9.3f} {(best.cost + best.fees) / best.shares:12.3f} {best.per_share:+12.4f} {best.shares:7.0f}")
            time.sleep(args.every)
    print(f"\n{n} snapshots; naive top-of-book sum below $1.00 in {naive}; cross-venue hedge profitable after fees at real depth in {allin}")
    print(f"same-venue: Kalshi ladder arb in {ladder_hits} snapshots; Polymarket Up+Down below $1 after fees in {set_hits}")
    if best_all:
        b = best_all
        print(f"best ever seen: {b.poly_side}+{b.kalshi_side} at strike {b.strike:,.0f} (gap ${b.gap:,.0f}): {b.per_share:+.4f} per share, {b.shares:.0f} shares, worst-case {b.profit:+.2f} USD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
