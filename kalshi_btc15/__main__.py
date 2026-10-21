"""python -m kalshi_btc15 probe | backtest [--days N] ...

probe     print the raw Kalshi / Coinbase responses this code depends on (fees, rules text, field names).
backtest  download (cached) markets, candles and BTC spot, then write the report. Read-only: no key, no orders.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import api, spot as spot_mod
from .data import build_samples
from .report import render
from .strategies import Config


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def attempt(label: str, fn):
    try:
        out = fn()
    except Exception as e:  # a probe reports failures, it does not stop on them
        print(f"\n== {label}: FAILED: {e}")
        return None
    print(f"\n== {label}\n{json.dumps(out, indent=1)[:2500]}")
    return out


def probe() -> int:
    attempt("series", lambda: api.get_json(f"{api.BASE}/series/{api.SERIES}"))
    body = attempt("one settled market (check floor_strike, open/close_time, rules_primary: is the result a 60 s average?)",
                   lambda: api.get_json(f"{api.BASE}/markets", {"series_ticker": api.SERIES, "status": "settled", "limit": 2}))
    attempt("historical cutoff", lambda: api.get_json(f"{api.BASE}/historical/cutoff"))
    markets = (body or {}).get("markets") or []
    if markets:
        m = api.parse_market(markets[0])
        if m:
            attempt("first 3 candles of that market", lambda: api.get_json(
                f"{api.BASE}/series/{api.SERIES}/markets/{m.ticker}/candlesticks",
                {"start_ts": m.close_ts - 960, "end_ts": m.close_ts + 60, "period_interval": 1}).get("candlesticks", [])[:3])
    now = int(time.time()) // 60 * 60
    attempt("Coinbase BTC-USD, last 3 minute candles [time, low, high, open, close, volume]", lambda: api.get_json(
        f"{spot_mod.BASE}/products/{spot_mod.PRODUCT}/candles",
        {"granularity": 60, "start": spot_mod.iso(now - 240), "end": spot_mod.iso(now - 60)}))
    return 0


def backtest(args) -> int:
    now = int(time.time())
    since = now - args.days * 86400
    cfg = Config(contracts=args.contracts, edge_cents=args.edge)
    try:
        info = api.series_info()
        say(f"series: {info.get('title')} fee_type={info.get('fee_type')} fee_multiplier={info.get('fee_multiplier')}")
        if args.fee_multiplier is None and info.get("fee_multiplier") is not None:
            cfg = replace(cfg, fee_multiplier=float(info["fee_multiplier"]))
        if info.get("fee_type") not in (None, "quadratic", "quadratic_with_maker_fees"):
            say(f"WARNING: fee_type {info.get('fee_type')!r} is not the quadratic schedule this backtest models")
    except api.ApiError as e:
        say(f"could not read series info ({e}); assuming the standard fee")
    if args.fee_multiplier is not None:
        cfg = replace(cfg, fee_multiplier=args.fee_multiplier)

    say("listing settled markets ...")
    markets = [m for m in api.settled_markets(refresh=args.refresh) if since <= m.close_ts <= now - 1200]
    if not markets:
        say("no settled markets in range")
        return 1
    say(f"{len(markets)} markets; downloading candles (cached) ...")
    api.prefetch_candles(api.SERIES, markets, progress=lambda i, n, bad: say(f"  candles {i}/{n} (failed {bad})"))

    spot = None
    if not args.no_spot:
        say("downloading BTC-USD minutes from Coinbase (cached) ...")
        try:
            spot = spot_mod.SpotSeries.fetch(markets[0].close_ts - 2 * 3600, markets[-1].close_ts + 300,
                                             progress=lambda i, n: say(f"  spot chunks {i}/{n}"))
        except api.ApiError as e:
            say(f"BTC data unavailable ({e}); running the Kalshi-only strategies")

    by_lead, skipped = {}, None
    for lead in args.leads:
        by_lead[lead], sk = build_samples(markets, lambda m: api.candles(api.SERIES, m), spot, lead_s=lead)
        skipped = skipped if skipped is not None else sk
    primary = by_lead[args.leads[0]]
    if not primary:
        say("no usable samples")
        return 1

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    report = render(primary, skipped, by_lead, cfg, len(markets), f"Kalshi KXBTC15M late-window backtest ({args.days} days to {day})")
    out = Path(args.out) if args.out else Path(__file__).with_name("reports") / f"report_{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    say(f"written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kalshi_btc15", description=__doc__.split("\n\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="print raw API responses to check assumptions")
    bt = sub.add_parser("backtest", help="download data and write the report")
    bt.add_argument("--days", type=int, default=400, help="how far back to go (default 400: the whole history, which starts 2025-12-10)")
    bt.add_argument("--leads", type=lambda t: [int(x) for x in t.split(",")], default=[120, 180, 240, 300],
                    help="seconds before close to enter; the first is the pre-registered one (default 120,180,240,300)")
    bt.add_argument("--contracts", type=int, default=10)
    bt.add_argument("--edge", type=float, default=3.0, help="model_edge minimum edge in cents (pre-registered: 3)")
    bt.add_argument("--fee-multiplier", type=float, default=None, help="override the series' fee multiplier")
    bt.add_argument("--no-spot", action="store_true", help="skip Coinbase; only the Kalshi-only strategies run")
    bt.add_argument("--refresh", action="store_true", help="re-list markets instead of using the cached list")
    bt.add_argument("--out", help="report path (default kalshi_btc15/reports/report_<date>.md)")
    args = ap.parse_args(argv)
    return probe() if args.cmd == "probe" else backtest(args)


if __name__ == "__main__":
    sys.exit(main())
