"""Usage:
    python -m xrpl_arb                    # real XRPL mainnet data, PAPER trading, dashboard at http://127.0.0.1:8780
    python -m xrpl_arb --demo             # synthetic markets (nothing real), for trying the dashboard offline
    python -m xrpl_arb --trade-live --max-trade 20     # REAL orders from the wallet in $XRPL_SEED; read xrpl_arb/README.md first

Paper mode holds no wallet or key and sends nothing: it only reads public ledger data.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from dataclasses import replace

from .bot import Bot
from .config import DEFAULT_RPC, KNOWN, Config
from .feed import DemoFeed, LiveFeed
from .paper import PaperExecutor
from .rpc import Rpc
from .server import make_server
from .state import BotState


def parse_markets(text: str) -> tuple[tuple[str, str], ...]:
    out = []
    for part in text.split(","):
        a, sep, b = part.strip().partition("/")
        if not sep:
            raise SystemExit(f"--markets wants pairs like XRP/RLUSD,XRP/USD.Bitstamp; got {part!r}")
        out.append((a, b))
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xrpl_arb", description="XRP Ledger arbitrage scanner and bot with a live dashboard")
    p.add_argument("--demo", action="store_true", help="synthetic markets instead of the real ledger")
    p.add_argument("--demo-interval", type=float, default=2.5, help="with --demo: seconds per synthetic ledger")
    p.add_argument("--rpc", action="append", metavar="URL", help="XRPL JSON-RPC server (repeatable; default: public servers)")
    p.add_argument("--base", default=Config.base, help=f"asset every cycle starts and ends in, and profit is counted in (default {Config.base}; known: {', '.join(KNOWN)})")
    p.add_argument("--markets", help="pairs to watch, e.g. XRP/RLUSD,XRP/USD.Bitstamp,RLUSD/USD.Bitstamp")
    p.add_argument("--max-hops", type=int, default=Config.max_hops)
    p.add_argument("--size-min", type=float, default=Config.size_min, help="smallest trade, in base units")
    p.add_argument("--size-max", type=float, default=Config.size_max, help="largest trade, in base units")
    p.add_argument("--min-profit-bps", type=float, default=Config.min_profit_bps)
    p.add_argument("--min-profit", type=float, default=Config.min_profit_base, help="smallest net profit per trade, in base units")
    p.add_argument("--trade-live", action="store_true", help="send REAL transactions (needs $XRPL_SEED and --max-trade)")
    p.add_argument("--max-trade", type=float, help="with --trade-live: hard cap on the base amount risked in one cycle")
    p.add_argument("--max-session-loss", type=float, default=None, help="with --trade-live: halt after this much realized loss (base units)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--ledgers", type=int, help="stop after this many ledgers and print a summary (no dashboard needed)")
    args = p.parse_args(argv)

    try:
        cfg = Config(base=args.base, markets=parse_markets(args.markets) if args.markets else Config.markets, max_hops=args.max_hops,
                     size_min=args.size_min, size_max=args.size_max, min_profit_bps=args.min_profit_bps, min_profit_base=args.min_profit).validate()
    except ValueError as e:
        raise SystemExit(f"bad settings: {e}")
    if args.demo and args.trade_live:
        raise SystemExit("--demo cannot trade live")

    assets = cfg.assets()
    rpc = Rpc(tuple(args.rpc) if args.rpc else DEFAULT_RPC)
    mode = "live" if args.trade_live else "paper"
    log_sink: list = []
    state = BotState(cfg, mode, "demo" if args.demo else "xrpl", node="synthetic" if args.demo else rpc.url)
    state.names = {a.key: n for n, a in KNOWN.items()}

    if args.trade_live:
        from .live import LiveExecutor, LiveSetupError

        seed = os.environ.get("XRPL_SEED")
        if not seed:
            raise SystemExit("--trade-live needs the wallet seed in the environment variable XRPL_SEED (never on the command line)")
        if not args.max_trade or args.max_trade <= 0:
            raise SystemExit("--trade-live needs --max-trade N: the most base units one cycle may risk")
        cfg = replace(cfg, size_max=min(cfg.size_max, args.max_trade), size_min=min(cfg.size_min, args.max_trade)).validate()
        state.cfg = cfg
        try:
            executor = LiveExecutor(cfg, assets[cfg.base], state, rpc, seed, max_session_loss=args.max_session_loss)
        except LiveSetupError as e:
            raise SystemExit(f"cannot start live trading: {e}")
        feed = LiveFeed(cfg, rpc, log=state.log, skip_owner=executor.address)
        size_cap = executor.spendable_base
    else:
        executor = PaperExecutor(assets[cfg.base], state)
        feed = DemoFeed(cfg, interval=args.demo_interval) if args.demo else LiveFeed(cfg, rpc, log=state.log)
        size_cap = None

    bot = Bot(cfg, feed, state, executor, size_cap=size_cap)
    stop = threading.Event()
    if args.trade_live:
        try:
            executor.selftest(bot.routes)
        except LiveSetupError as e:
            raise SystemExit(f"cannot start live trading: {e}")
        print(f"\nLIVE TRADING with {executor.address}: {executor.spendable_base():.4f} {cfg.base} spendable, at most {cfg.size_max:g} per cycle.")
        print("Each cycle is several separate transactions; the ledger cannot make them atomic. Starting in 10 seconds: Ctrl+C to abort.")
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            raise SystemExit("aborted")

    if args.ledgers:
        seen = 0
        t = threading.Thread(target=bot.run, args=(stop,), daemon=True)
        t.start()
        try:
            while state.stats["ledgers"] < args.ledgers:
                state.wait(state.version, 5.0)
        except KeyboardInterrupt:
            pass
        stop.set()
        print_summary(state)
        return 0

    server = make_server(state, args.host, args.port)
    threading.Thread(target=bot.run, args=(stop,), daemon=True).start()
    url = f"http://{args.host}:{server.server_address[1]}"
    print(f"xrpl_arb dashboard: {url}   (data: {state.source}; trading: {mode.upper()}; Ctrl+C to stop)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if hasattr(feed, "close"):
            feed.close()
    return 0


def print_summary(state: BotState) -> None:
    s = state.stats
    print(f"ledgers scanned {s['ledgers']}   route evaluations {s['routes_seen']}   with positive net profit {s['profitable']}   above threshold {s['executable']}")
    print(f"best edge seen {s['best_edge_bps']} bps on {s['best_edge_route']}")
    print(f"paper trades: opened {s['opened']} won {s['won']} lost {s['lost']} missed {s['missed']} stuck {s['stuck']}   realized {s['pnl_real']:+.4f}   theoretical {s['pnl_theory']:+.4f}")
    for r in state.routes[:8]:
        if "edge_bps" in r:
            print(f"  {r['route']:42s} {r['status']:16s} size {r['size']:8.2f}  profit {r['profit']:+9.4f}  edge {r['edge_bps']:+8.2f} bps")


if __name__ == "__main__":
    raise SystemExit(main())
