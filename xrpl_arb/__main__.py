"""Usage:
    python -m xrpl_arb scan --config xrpl_arb/markets.example.json            # one scan
    python -m xrpl_arb scan --config FILE --loop 15 --out data/xrpl_opps.jsonl  # every 15 s, recorded
    python -m xrpl_arb watch --config FILE --out data/xrpl_watch.jsonl          # every ledger close, via websocket
    python -m xrpl_arb ui --config FILE                                         # the same, with a dashboard in your browser

Read-only: asks public rippled servers for order books and AMM pools and reports XRP -> ... -> XRP
cycles that would return more than they cost. It never signs or submits anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from .client import DEFAULT_URLS, XrplClient, XrplError
from .cycles import Opportunity, candidates, closest, passes
from .model import XRP, Asset, is_valid_address
from .snapshot import RateCache, take_snapshot
from .stream import DEFAULT_WS, watch
from .ui import DashboardState, serve


def load_config(path: str) -> tuple[list[Asset], list[tuple[Asset, Asset]] | None]:
    """(assets, pairs). With a "pairs" list only those pairs are read and "assets" is ignored; otherwise
    every pair among XRP and "assets" is."""
    try:
        cfg = json.loads(Path(path).read_text())
        if "pairs" in cfg:
            pairs = [tuple(Asset.parse(s) for s in p) for p in cfg["pairs"]]
            if not pairs or any(len(p) != 2 or p[0] == p[1] for p in pairs):
                raise ValueError('"pairs" must be a non-empty list of two different assets')
            return [a for p in pairs for a in p if not a.is_xrp], pairs
        assets = [Asset.parse(s) for s in cfg["assets"]]
        if not assets:
            raise ValueError("no assets")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"bad config {path}: {e!r}" if isinstance(e, KeyError) else f"bad config {path}: {e}")
    return assets, None


def describe(opp: Opportunity) -> str:
    path = " -> ".join(Asset.parse(p).short() for p in opp.path)
    return (f"  {opp.profit:+.6f} XRP ({opp.profit_pct:+.3f}%)  in {opp.amount_in:f} out {opp.amount_out:f}  {path}")


def one_scan(client: XrplClient, config, rates: RateCache, args, sink) -> list[Opportunity]:
    assets, pairs = config
    snap = take_snapshot(client, assets, rates, args.book_limit, pairs, args.workers, args.account)
    min_profit, min_pct = Decimal(str(args.min_profit)), Decimal(str(args.min_profit_pct))
    cands = candidates(snap.markets, max_xrp=Decimal(str(args.max_xrp)), tx_cost=Decimal(str(args.tx_cost)),
                       max_legs=args.max_legs)
    opps = [o for o in cands if passes(o, min_profit, min_pct)]
    now = round(time.time(), 1)
    note = "" if snap.consistent else " (some reads fell back to a newer ledger)"
    note += f" ({len(snap.skipped)} unreadable responses skipped)" if snap.skipped else ""
    print(f"ledger {snap.ledger}: {snap.books} books with offers, {snap.pools} pools, {len(opps)} opportunities{note}", flush=True)
    for opp in opps:
        print(describe(opp), flush=True)
    near = None  # how close a scan that found nothing came, so "0" can be told from "almost"
    if not opps and cands:
        print("  best below your thresholds: " + describe(cands[0]).lstrip(), flush=True)
        near = cands[0]
    elif not opps:
        spot = closest(snap.markets, args.max_legs)
        if spot:
            print(f"  nothing pays at any size; nearest cycle returns {spot[0]:+.3f}% at vanishing size: "
                  f"{' -> '.join(m.asset_in.short() for m in spot[1])} -> XRP", flush=True)
    for line in snap.skipped[:5]:
        print(f"  skipped {line}", file=sys.stderr, flush=True)
    if sink:
        rows = [{"type": "scan", "t": now, "ledger": snap.ledger, "consistent": snap.consistent,
                 "books": snap.books, "pools": snap.pools, "skipped": len(snap.skipped), "opportunities": len(opps),
                 "candidates": len(cands), "best_below_thresholds": near.to_json() if near else None}]
        rows += [{"type": "opp", "t": now, "ledger": snap.ledger, **o.to_json()} for o in opps]
        sink.write("".join(json.dumps(r) + "\n" for r in rows))
        sink.flush()
    return opps


def add_common(s: argparse.ArgumentParser) -> None:
    s.add_argument("--config", required=True,
                   help="JSON file: an 'assets' list of CUR+issuer strings, or an explicit 'pairs' list of two-asset lists")
    s.add_argument("--rpc", nargs="+", default=list(DEFAULT_URLS), help="rippled JSON-RPC URLs, tried in turn")
    s.add_argument("--out", help="append scan and opportunity rows to this JSONL file")
    s.add_argument("--max-xrp", type=float, default=100.0, help="largest XRP input to consider")
    s.add_argument("--max-legs", type=int, default=4, choices=(2, 3, 4, 5))
    s.add_argument("--tx-cost", type=float, default=0.0001,
                   help="network fee in XRP charged to each cycle (the site's bot pays 40-111 drops per batch)")
    s.add_argument("--min-profit", type=float, default=0.001, help="minimum profit in XRP after the fee")
    s.add_argument("--min-profit-pct", type=float, default=0.25,
                   help="minimum profit as %% of the input. The default sits above the worst over-estimate seen against "
                   "the ledger's own quotes (0.24%%); it is also the margin for the ledger moving before a payment lands")
    s.add_argument("--book-limit", type=int, default=60, help="offers read per order book")
    s.add_argument("--workers", type=int, default=6, help="parallel requests per read")
    s.add_argument("--account", help="the wallet that would trade: it pays an AMM's discounted fee where it holds, or is "
                   "authorized on, the live auction slot (default: everyone pays the full fee)")


def open_sink(path: str | None):
    if not path:
        return None
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a")


def ui_settings(args) -> dict:
    return {"min_profit": args.min_profit, "min_profit_pct": args.min_profit_pct, "tx_cost": args.tx_cost,
            "max_legs": args.max_legs, "max_xrp": args.max_xrp, "account": args.account}


def run_ui(args, config, client, sink, connect) -> int:
    assets, pairs = config
    if pairs is None:
        pairs = list(combinations([XRP] + [a for a in assets if not a.is_xrp], 2))
    if args.replay:
        with open(args.replay) as f:
            dashboard = DashboardState.from_rows([json.loads(line) for line in f], ui_settings(args))
    else:
        dashboard = DashboardState(ui_settings(args))
    url = f"http://127.0.0.1:{args.port}"

    def ready(server):
        print(f"dashboard at {url}  (Ctrl-C to stop)", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    if not args.replay:
        def run_watch():
            try:
                watch(client, pairs, args, sink=sink, observer=dashboard.observe, log=lambda line: print(line, flush=True),
                      **({"connect": connect} if connect else {}))
            finally:
                dashboard.observe({"kind": "status", "state": "stopped", "detail": "watcher finished"})
        threading.Thread(target=run_watch, daemon=True).start()
    try:
        serve(dashboard, args.port, ready)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None, client: XrplClient | None = None, connect=None) -> int:
    p = argparse.ArgumentParser(prog="python -m xrpl_arb", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="look for cycle arbitrage by polling (read-only)")
    add_common(s)
    s.add_argument("--loop", type=float, default=0, metavar="SECONDS", help="repeat forever with this pause (default: scan once)")
    w = sub.add_parser("watch", help="re-check at every ledger close, driven by a websocket (read-only)")
    add_common(w)
    w.add_argument("--ws", nargs="+", default=list(DEFAULT_WS), help="rippled websocket URLs, tried in turn")
    w.add_argument("--settle", type=float, default=0.35,
                   help="seconds to keep collecting transactions after a ledger closes before reading")
    w.add_argument("--full-every", type=int, default=20, help="re-read every pair this often, as a safety net")
    w.add_argument("--minutes", type=float, default=0, help="stop after this long (default: run until interrupted)")
    w.add_argument("--max-ledgers", type=int, default=0, help="stop after this many ledgers (default: no limit)")
    u = sub.add_parser("ui", help="watch, with a live dashboard on http://127.0.0.1:PORT (read-only)")
    add_common(u)
    u.add_argument("--ws", nargs="+", default=list(DEFAULT_WS), help="rippled websocket URLs, tried in turn")
    u.add_argument("--settle", type=float, default=0.35)
    u.add_argument("--full-every", type=int, default=20)
    u.add_argument("--minutes", type=float, default=0, help="stop watching after this long; the page stays up")
    u.add_argument("--max-ledgers", type=int, default=0)
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    u.add_argument("--replay", metavar="JSONL", help="show a recorded watch run (its --out file) instead of watching")
    args = p.parse_args(argv)
    if args.account and not is_valid_address(args.account):
        p.error(f"--account {args.account!r} is not a valid XRPL address")

    config = load_config(args.config)
    client = client or XrplClient(args.rpc)
    sink = open_sink(args.out)
    try:
        if args.cmd == "ui":
            return run_ui(args, config, client, sink, connect)
        if args.cmd == "watch":
            assets, pairs = config
            if pairs is None:
                pairs = list(combinations([XRP] + [a for a in assets if not a.is_xrp], 2))
            return watch(client, pairs, args, sink=sink, **({"connect": connect} if connect else {}))
        rates = RateCache(client)
        while True:
            try:
                one_scan(client, config, rates, args, sink)
            except (XrplError, OSError) as e:
                if not args.loop:
                    print(f"scan failed: {e}", file=sys.stderr)
                    return 1
                print(f"scan failed, retrying: {e}", file=sys.stderr)
            if not args.loop:
                return 0
            time.sleep(args.loop)
    except KeyboardInterrupt:
        return 0
    finally:
        if sink:
            sink.close()


if __name__ == "__main__":
    sys.exit(main())
