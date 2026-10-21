"""Look at the markets without running the bot: `python -m xrpl_arb.markets [ASSET ASSET ...]`.

Prints, for each pair, the AMM pool (size, fee), the order book's best prices, and how far apart the two venues are right now."""

from __future__ import annotations

import argparse
import itertools
import sys

from .config import DEFAULT_RPC, KNOWN, Config
from .feed import LiveFeed
from .rpc import Rpc


def describe(feed: LiveFeed, snap, a, b, sample: float) -> list[str]:
    lines = []
    pool = snap.pools.get("|".join(sorted((a.key, b.key))))
    sell = snap.books.get((b.key, a.key)) or []  # pay b, receive a  -> asks for a
    buy = snap.books.get((a.key, b.key)) or []  # pay a, receive b  -> bids for a
    ask = sell[0].price if sell else None  # b per a
    bid = 1 / buy[0].price if buy else None  # b per a
    amm_mid = (pool.rb / pool.ra) if pool else None
    head = f"{a.code}/{b.code}"
    lines.append(f"{head:22s} AMM {'%.0f %s + %.0f %s (fee %.3f%%)' % (pool.ra, a.code, pool.rb, b.code, pool.fee * 100) if pool else 'none':58s}")
    lines.append(f"{'':22s} book: {len(buy)} bids / {len(sell)} asks   bid {bid if bid is None else format(bid, '.6g')}   ask {ask if ask is None else format(ask, '.6g')}   amm mid {amm_mid if amm_mid is None else format(amm_mid, '.6g')}")
    if pool and (bid or ask):
        mid = ((bid or ask) + (ask or bid)) / 2
        lines.append(f"{'':22s} amm vs book mid: {(amm_mid / mid - 1) * 1e4:+.1f} bps")
    for src, dst in ((a, b), (b, a)):
        hop = snap.hop(src, dst)
        if hop is None:
            continue
        size = sample if src.is_xrp else sample / 2
        q = hop.quote(size, snap.fresh_pools())
        price = q.out / q.spent if q.spent else 0
        lines.append(f"{'':22s} {src.code}→{dst.code} {size:g}: out {q.out:.6g} ({price:.6g}/unit), AMM share {q.amm_share * 100:.0f}%{' [exhausted]' if q.exhausted else ''}")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xrpl_arb.markets", description="Show AMM and order-book state for XRPL pairs")
    p.add_argument("assets", nargs="*", help=f"asset names to pair up (default: all known: {', '.join(KNOWN)})")
    p.add_argument("--rpc", help="JSON-RPC URL (default: public servers)")
    p.add_argument("--sample", type=float, default=100.0, help="sample trade size for the quote (default 100)")
    args = p.parse_args(argv)
    names = args.assets or list(KNOWN)
    pairs = [(x, y) for x, y in itertools.combinations(names, 2) if not (KNOWN[x].issuer == KNOWN[y].issuer and not KNOWN[x].is_xrp and KNOWN[x].code == KNOWN[y].code)]
    cfg = Config(markets=tuple(pairs), book_depth=100)
    rpc = Rpc((args.rpc,) if args.rpc else DEFAULT_RPC)
    feed = LiveFeed(cfg, rpc, log=lambda lvl, msg: print(f"  [{lvl}] {msg}", file=sys.stderr))
    ledger, close = feed.latest()
    snap = feed.snapshot(ledger, close)
    print(f"ledger {ledger}   server {rpc.url}   transfer rates: " + ", ".join(f"{KNOWN_NAME.get(k, k)} {r:.4f}" for k, r in snap.rates.items() if r != 1.0) + "\n")
    for a, b in feed.pairs:
        has = ("|".join(sorted((a.key, b.key))) in snap.pools) or snap.books.get((a.key, b.key)) or snap.books.get((b.key, a.key))
        if has:
            print("\n".join(describe(feed, snap, a, b, args.sample)))
    feed.close()
    return 0


KNOWN_NAME = {a.key: n for n, a in KNOWN.items()}

if __name__ == "__main__":
    raise SystemExit(main())
