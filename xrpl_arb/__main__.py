"""Usage:
    python -m xrpl_arb scan --config xrpl_arb/markets.example.json            # one scan
    python -m xrpl_arb scan --config FILE --loop 15 --out data/xrpl_opps.jsonl  # every 15 s, recorded

Read-only: asks public rippled servers for order books and AMM pools and reports XRP -> ... -> XRP
cycles that would return more than they cost. It never signs or submits anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from .client import DEFAULT_URLS, XrplClient, XrplError
from .cycles import Opportunity, scan
from .model import Asset, is_valid_address
from .snapshot import RateCache, take_snapshot


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
    opps = scan(snap.markets, max_xrp=Decimal(str(args.max_xrp)), tx_cost=Decimal(str(args.tx_cost)),
                min_profit=Decimal(str(args.min_profit)), min_profit_pct=Decimal(str(args.min_profit_pct)),
                max_legs=args.max_legs)
    now = round(time.time(), 1)
    note = "" if snap.consistent else " (some reads fell back to a newer ledger)"
    note += f" ({len(snap.skipped)} unreadable responses skipped)" if snap.skipped else ""
    print(f"ledger {snap.ledger}: {snap.books} books with offers, {snap.pools} pools, {len(opps)} opportunities{note}", flush=True)
    for line in snap.skipped[:5]:
        print(f"  skipped {line}", file=sys.stderr, flush=True)
    for opp in opps:
        print(describe(opp), flush=True)
    if sink:
        rows = [{"type": "scan", "t": now, "ledger": snap.ledger, "consistent": snap.consistent,
                 "books": snap.books, "pools": snap.pools, "skipped": len(snap.skipped), "opportunities": len(opps)}]
        rows += [{"type": "opp", "t": now, "ledger": snap.ledger, **o.to_json()} for o in opps]
        sink.write("".join(json.dumps(r) + "\n" for r in rows))
        sink.flush()
    return opps


def main(argv=None, client: XrplClient | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m xrpl_arb", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="look for cycle arbitrage (read-only)")
    s.add_argument("--config", required=True,
                   help="JSON file: an 'assets' list of CUR+issuer strings, or an explicit 'pairs' list of two-asset lists")
    s.add_argument("--rpc", nargs="+", default=list(DEFAULT_URLS), help="rippled JSON-RPC URLs, tried in turn")
    s.add_argument("--loop", type=float, default=0, metavar="SECONDS", help="repeat forever with this pause (default: scan once)")
    s.add_argument("--out", help="append scan and opportunity rows to this JSONL file")
    s.add_argument("--max-xrp", type=float, default=100.0, help="largest XRP input to consider")
    s.add_argument("--max-legs", type=int, default=4, choices=(2, 3, 4, 5))
    s.add_argument("--tx-cost", type=float, default=0.0001,
                   help="network fee in XRP charged to each cycle (the site's bot pays 40-111 drops per batch)")
    s.add_argument("--min-profit", type=float, default=0.001, help="minimum profit in XRP after the fee")
    s.add_argument("--min-profit-pct", type=float, default=0.1,
                   help="minimum profit as %% of the input: the margin for the ledger moving before a payment lands")
    s.add_argument("--book-limit", type=int, default=60, help="offers read per order book")
    s.add_argument("--workers", type=int, default=6, help="parallel requests per scan")
    s.add_argument("--account", help="the wallet that would trade: it pays an AMM's discounted fee where it holds, or is "
                   "authorized on, the live auction slot (default: everyone pays the full fee)")
    args = p.parse_args(argv)
    if args.account and not is_valid_address(args.account):
        p.error(f"--account {args.account!r} is not a valid XRPL address")

    config = load_config(args.config)
    client = client or XrplClient(args.rpc)
    rates = RateCache(client)
    sink = None
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        sink = open(args.out, "a")
    try:
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
