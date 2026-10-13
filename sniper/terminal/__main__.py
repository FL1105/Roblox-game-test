"""Usage:
    python -m sniper.terminal                       # demo: synthetic market, nothing real
    python -m sniper.terminal --replay launches.jsonl
    python -m sniper.terminal --live --rpc URL      # real launches, still paper trading

Paper trading only: there is no wallet, key or order-sending code anywhere in this program.
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser

from ..pump.events import read_jsonl
from ..pump.store import LaunchStore
from .feeds import DemoFeed, LiveFeed, ReplayFeed, TeeSink, start_feed
from .server import make_server
from .state import TerminalState


def build(args) -> tuple[TerminalState, object, callable]:
    """Returns (state, feed, cleanup)."""
    if args.replay:
        events = read_jsonl(args.replay)
        if not events:
            raise SystemExit(f"{args.replay} has no events")
        return TerminalState(source=f"replay {os.path.basename(args.replay)}", simulated=False), ReplayFeed(events, args.speed), lambda: None

    if args.live:
        from ..listener.__main__ import DEFAULT_RPC, make_scan_fn
        from ..listener.live import JsonlSink, Listener
        from ..scanner.sources import SolanaRpc

        state = TerminalState(source="live pump.fun", simulated=False)
        store = LaunchStore(args.db)
        jsonl = JsonlSink(args.events) if args.events else None
        rpc = SolanaRpc(args.rpc or os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC))
        listener = Listener(TeeSink(state.emit, jsonl), store, make_scan_fn(rpc, store, wallet_lookup=True))

        def cleanup():
            store.close()
            if jsonl:
                jsonl.close()

        return state, LiveFeed(listener), cleanup

    return TerminalState(source="demo", simulated=True), DemoFeed(speed=args.speed), lambda: None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sniper.terminal", description="Paper-trading terminal for pump.fun launches")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--replay", metavar="FILE", help="replay a JSONL recording made by sniper.listener")
    mode.add_argument("--live", action="store_true", help="follow real launches (needs 'pip install websockets')")
    p.add_argument("--speed", type=float, default=5.0, help="replay/demo speed multiplier (default 5)")
    p.add_argument("--rpc", help="Solana RPC URL for --live (default: $SOLANA_RPC_URL)")
    p.add_argument("--events", help="with --live: also append events to this JSONL file")
    p.add_argument("--db", default="launches.db", help="with --live: deployer-history database")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)

    state, feed, cleanup = build(args)
    server = make_server(state, args.host, args.port)
    stop = threading.Event()
    start_feed(feed, state, stop)

    url = f"http://{args.host}:{server.server_address[1]}"
    print(f"Terminal running at {url}   (source: {state.source}; Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
