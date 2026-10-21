"""python -m pmarb scan | watch   Read-only prediction-market arbitrage research (Kalshi and Polymarket). No keys, no orders."""

from __future__ import annotations

import argparse
import sys
import time

from . import match, scan, venues, watch


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def scan_cmd(args) -> int:
    t0 = time.time()
    progress = lambda s: say(f"  [{time.time() - t0:4.0f}s] {s}")
    for name, snap in (("Kalshi", lambda: venues.kalshi_snapshot(progress)), ("Polymarket", lambda: venues.poly_snapshot(progress))):
        events = snap()
        summary = scan.summarise(scan.evaluate(events))
        print(f"\n== {name}: {len(events)} events, {sum(len(e.quotes) for e in events)} quotes")
        for kind, d in summary.items():
            note = "  (unverified: needs an exhaustive outcome set, so not an arbitrage on its own)" if kind == "buy_all" else ""
            print(f"  {kind:12s} priced {d['priced']:6d} | profitable after fees {d['profitable']:4d}{note}")
            for o in d["top"][:args.top]:
                print("     ", scan.describe(o))
    return 0


def watch_cmd(args) -> int:
    fees = venues.kalshi_fees()
    say("fetching Polymarket sports events and Kalshi events to pair games ...")
    kalshi = venues.kalshi_snapshot()
    pairs = watch.game_pairs(match.pair_games(kalshi, venues.poly_sports_events()), fees)
    say(f"{len(pairs)} matched team-sides; polling every {args.interval}s for {args.minutes} min -> {args.log}")
    watch.watch(pairs, fees, args.minutes, args.interval, args.log, say)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pmarb", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="one full pass over every open market on both venues")
    sc.add_argument("--top", type=int, default=5)
    wa = sub.add_parser("watch", help="poll matched games and live BTC 15-minute windows and log every cross-venue gap")
    wa.add_argument("--minutes", type=float, default=30)
    wa.add_argument("--interval", type=float, default=20)
    wa.add_argument("--log", default="pmarb_watch.jsonl")
    args = ap.parse_args(argv)
    return {"scan": scan_cmd, "watch": watch_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
