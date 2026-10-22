"""python -m pmarb scan | watch   Read-only prediction-market arbitrage research (Kalshi and Polymarket). No keys, no orders."""

from __future__ import annotations

import argparse
import sys
import time

from . import bankroll, btc3, match, scan, sizing, summary, venues, watch


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


def bankroll_cmd(args) -> int:
    print("Kalshi taker fee per contract (cents), rounded up on the whole order")
    sizes = [1, 5, 10, 25, 100, 1000]
    print(f"{'price':>6s} " + " ".join(f"q={q:<6d}" for q in sizes))
    for p in (0.05, 0.30, 0.50, 0.90, 0.97):
        print(f"{p:6.2f} " + " ".join(f"{100 * sizing.fee_per_contract(p, q):8.2f}" for q in sizes))
    if args.log:
        rows = summary.load(args.log)
        print("\nLive-game dislocations in the log, first-sight entry, capital locked (a ceiling: instant fills at displayed size)")
        print(f"{'bankroll':>9s} {'trades':>7s} {'net $':>9s} {'return':>8s}")
        for b in args.bankrolls:
            taken = bankroll.simulate(rows, b)
            net = sum(x[3] for x in taken)
            print(f"{b:9,.0f} {len(taken):7d} {net:9.2f} {100 * net / b:7.2f}%")
    return 0


def btc3_cmd(args) -> int:
    btc3.watch(args.minutes, args.interval, args.log, say)
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
    bk = sub.add_parser("bankroll", help="fee rounding by order size, and what a bankroll could have captured from a watch log")
    bk.add_argument("--log", help="a watch log (jsonl)")
    bk.add_argument("--bankrolls", type=lambda t: [float(x) for x in t.split(",")], default=[50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000])
    b3 = sub.add_parser("btc3", help="the same BTC 15-minute window on Kalshi, Polymarket and Limitless")
    b3.add_argument("--minutes", type=float, default=45)
    b3.add_argument("--interval", type=float, default=15)
    b3.add_argument("--log", default="pmarb_btc3.jsonl")
    args = ap.parse_args(argv)
    return {"scan": scan_cmd, "watch": watch_cmd, "bankroll": bankroll_cmd, "btc3": btc3_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
