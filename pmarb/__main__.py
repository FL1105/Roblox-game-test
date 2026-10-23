"""python -m pmarb scan | watch   Read-only prediction-market arbitrage research (Kalshi and Polymarket). No keys, no orders."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from . import band, bankroll, bot, btc3, dashboard, match, scan, sizing, summary, venues, watch


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


def band_cmd(args) -> int:
    print(band.run(args.windows, args.history, say))
    return 0


def bot_cmd(args) -> int:
    if args.live:
        print("Live execution is not implemented: it needs your own exchange keys and cannot be tested without them. Running as a paper bot.", file=sys.stderr)
    limits = bot.Limits(max_sets=args.max_sets, max_window_cost=args.max_window_cost, max_open_cost=args.max_open_cost, daily_loss_stop=-abs(args.loss_stop))
    paper = bot.PaperBot(limits, args.ledger)
    bot.run(args.minutes, args.interval, paper, say)
    return 0


def ui_cmd(args) -> int:
    limits = bot.Limits(max_sets=args.max_sets, max_window_cost=args.max_window_cost, max_open_cost=args.max_open_cost, daily_loss_stop=-abs(args.loss_stop))
    paper = bot.PaperBot(limits, args.ledger)
    restored = paper.restore(args.ledger)
    rt = dashboard.Runtime(paper, args.interval, args.ledger + ".actuals.json")
    server = dashboard.make_server(rt, "127.0.0.1", args.port)
    threading.Thread(target=rt.run, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"PAPER bot + live dashboard at {url}  (ledger {args.ledger}, {restored} lines restored). No orders are placed. Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        rt.stop.set()
        server.server_close()
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
    bd = sub.add_parser("band", help="score the pre-registered 'sell the band' hedge on historical BTC 15-minute windows")
    bd.add_argument("--windows", required=True, help="json list of [close_ts, kalshi_up, poly_up]")
    bd.add_argument("--history", required=True, help="cache of Polymarket price histories (json)")
    bo = sub.add_parser("bot", help="PAPER-trade the Kalshi/Polymarket BTC 15-minute hedge against live books (places no orders)")
    bo.add_argument("--minutes", type=float, default=110)
    bo.add_argument("--interval", type=float, default=6)
    bo.add_argument("--ledger", default="pmarb_bot_ledger.jsonl")
    bo.add_argument("--max-sets", type=int, default=100)
    bo.add_argument("--max-window-cost", type=float, default=300.0)
    bo.add_argument("--max-open-cost", type=float, default=1000.0)
    bo.add_argument("--loss-stop", type=float, default=300.0)
    bo.add_argument("--live", action="store_true", help="not implemented; accepted only to say so")
    ui = sub.add_parser("ui", help="run the PAPER bot with a live P&L dashboard in your browser (loopback only; places no orders)")
    ui.add_argument("--port", type=int, default=8766)
    ui.add_argument("--interval", type=float, default=6)
    ui.add_argument("--ledger", default="pmarb_bot_ledger.jsonl", help="appended to; also restored from on start")
    ui.add_argument("--max-sets", type=int, default=100)
    ui.add_argument("--max-window-cost", type=float, default=300.0)
    ui.add_argument("--max-open-cost", type=float, default=1000.0)
    ui.add_argument("--loss-stop", type=float, default=300.0, help="stop opening trades once today's realised loss reaches this many dollars")
    ui.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    return {"ui": ui_cmd, "bot": bot_cmd, "band": band_cmd, "scan": scan_cmd, "watch": watch_cmd, "bankroll": bankroll_cmd, "btc3": btc3_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
