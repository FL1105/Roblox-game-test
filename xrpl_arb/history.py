"""Replay real ledger history through the same bot and paper executor: `python -m xrpl_arb.history --hours 48`.

Full-history XRPL servers keep every past order book and pool, so instead of waiting live this reads windows of consecutive past ledgers
(every `stride` ledgers, `window` ledgers each) and runs them through the normal per-ledger code. Inside a window a cycle seen at ledger N
has its first leg priced at N+1 and its next at N+2, exactly as in live paper mode, so the result includes the cost of being late.

What it cannot see: other bots racing for the same gap in the same ledger (a gap that is still there one ledger later was not taken in
that ledger, but your own transaction would also have had to be ordered ahead of theirs), or how long a real submission takes."""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .__main__ import parse_markets
from .bot import Bot
from .config import DEFAULT_RPC, Config
from .feed import Snapshot
from .paper import PaperExecutor
from .rpc import Rpc
from .state import BotState

LEDGERS_PER_HOUR = 3600 / 3.9  # the ledger closes every 3 to 4 seconds
SPACING_S = 3.9


def plan_windows(first: int, last: int, stride: int, window: int) -> list[int]:
    """Start ledger of every window that fits between `first` and `last`."""
    return list(range(first, last - window + 2, stride))


def fetch_window(feed, start: int, window: int) -> list[Snapshot]:
    t0 = feed.ledger_time(start) if hasattr(feed, "ledger_time") else 0.0
    return [feed.snapshot(start + i, t0 + i * SPACING_S) for i in range(window)]


def replay(cfg: Config, feed, starts: list[int], window: int, workers: int = 2, progress=None) -> tuple[list[dict], BotState]:
    """Run every window through a fresh-per-window trade slot but one shared set of statistics. Returns per-ledger route samples."""
    state = BotState(cfg, "paper", "history")
    executor = PaperExecutor(cfg.assets()[cfg.base], state)
    bot = Bot(cfg, feed, state, executor)
    samples: list[dict] = []
    skipped = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (start, snaps) in enumerate(zip(starts, pool.map(lambda s: _safe_fetch(feed, s, window), starts)), 1):
            if snaps is None:
                skipped += 1
                continue
            executor.open_trade = None  # nothing carries over between windows
            for i, snap in enumerate(snaps):
                state.running = i <= window - 1 - cfg.max_hops  # the last ledgers only finish trades: each leg needs its own ledger, so every opened trade completes inside its window
                bot.step(snap)
                for r in state.routes:
                    if "edge_bps" in r:
                        samples.append({"ledger": snap.ledger, "t": snap.close_time, "route": r["route"], "hops": r["hops"], "status": r["status"],
                                        "edge_bps": r["edge_bps"], "profit": r["profit"], "size": r["size"]})
            if progress:
                progress(n, len(starts), skipped)
    state.stats["windows_skipped"] = skipped
    return samples, state


def _safe_fetch(feed, start, window):
    try:
        return fetch_window(feed, start, window)
    except (OSError, ValueError, KeyError):
        return None


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else float("nan")


def summarize(samples: list[dict], stats: dict, starts: list[int], first_t: float, last_t: float, stride: int, window: int) -> dict:
    routes: dict[str, list[dict]] = {}
    for s in samples:
        routes.setdefault(s["route"], []).append(s)
    per_route = []
    for name, rows in routes.items():
        edges = [r["edge_bps"] for r in rows]
        per_route.append({"route": name, "ledgers": len(rows), "share_profitable": sum(1 for r in rows if r["profit"] > 0) / len(rows),
                          "share_executable": sum(1 for r in rows if r["status"] in ("EXECUTABLE", "COOLDOWN")) / len(rows),
                          "edge_bps_p50": pct(edges, 0.5), "edge_bps_p90": pct(edges, 0.9), "edge_bps_p99": pct(edges, 0.99), "edge_bps_max": max(edges),
                          "best_profit": max(r["profit"] for r in rows)})
    per_route.sort(key=lambda r: -r["edge_bps_p90"])
    best = sorted((s for s in samples if s["profit"] > 0), key=lambda s: -s["profit"])[:10]
    return {"windows": len(starts), "ledgers": len({s["ledger"] for s in samples}), "stride_ledgers": stride, "window_ledgers": window,
            "from": first_t, "to": last_t, "routes": per_route, "best_moments": best,
            "paper": {k: stats[k] for k in ("opened", "won", "lost", "missed", "stuck", "pnl_theory", "pnl_real", "fees", "windows_skipped")}}


def print_report(rep: dict, base: str) -> None:
    ts = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))
    print(f"\nReplayed {rep['windows']} windows ({rep['ledgers']:,} ledgers read, 1 window every {rep['stride_ledgers']} ledgers) from {ts(rep['from'])} to {ts(rep['to'])} UTC\n")
    print(f"{'route':44s}{'ledgers':>8s}{'profitable':>11s}{'tradable':>9s}{'edge p50':>10s}{'p90':>8s}{'p99':>8s}{'max':>8s}")
    for r in rep["routes"]:
        print(f"{r['route']:44s}{r['ledgers']:8d}{r['share_profitable'] * 100:10.1f}%{r['share_executable'] * 100:8.1f}%{r['edge_bps_p50']:+10.1f}{r['edge_bps_p90']:+8.1f}{r['edge_bps_p99']:+8.1f}{r['edge_bps_max']:+8.1f}")
    print("\n(edge in bps of trade size, net of fees; 'profitable' = any size nets above zero; 'tradable' = also clears the thresholds)")
    p = rep["paper"]
    print(f"\nPaper trades: opened {p['opened']}  won {p['won']}  lost {p['lost']}  missed {p['missed']}  stuck {p['stuck']}   realized {p['pnl_real']:+.4f} {base}   quoted at detection {p['pnl_theory']:+.4f} {base}   fees {p['fees']:.4f}")
    if rep["best_moments"]:
        print("\nLargest single-ledger opportunities seen:")
        for m in rep["best_moments"]:
            print(f"  {ts(m['t'])} UTC  ledger {m['ledger']}  {m['route']}  {m['edge_bps']:+.1f} bps  {m['profit']:+.4f} {base} at size {m['size']:.0f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xrpl_arb.history", description="Replay past XRP Ledger history through the arbitrage bot (read-only)")
    p.add_argument("--hours", type=float, default=24, help="how far back to sample (default 24)")
    p.add_argument("--ago-hours", type=float, default=0, help="end this long ago instead of now, to look at an older period")
    p.add_argument("--from-ledger", type=int, help="first ledger (overrides --hours)")
    p.add_argument("--to-ledger", type=int, help="last ledger")
    p.add_argument("--stride", type=int, default=200, help="ledgers between windows (default 200, about 13 minutes); 1 with a small --window reads every ledger")
    p.add_argument("--window", type=int, default=5, help="consecutive ledgers per window; needs at least max-hops + 1 so the longest trade can finish (default 5)")
    p.add_argument("--workers", type=int, default=2, help="windows fetched at once (default 2; public servers rate-limit)")
    p.add_argument("--rpc", action="append", metavar="URL", help="a FULL-HISTORY XRPL JSON-RPC server (default: public servers)")
    p.add_argument("--base", default=Config.base)
    p.add_argument("--markets", help="pairs to watch, e.g. XRP/RLUSD,XRP/SOLO")
    p.add_argument("--max-hops", type=int, default=Config.max_hops)
    p.add_argument("--size-min", type=float, default=Config.size_min)
    p.add_argument("--size-max", type=float, default=Config.size_max)
    p.add_argument("--min-profit-bps", type=float, default=Config.min_profit_bps)
    p.add_argument("--min-profit", type=float, default=Config.min_profit_base)
    p.add_argument("--out", help="also write the full report as JSON to this file")
    args = p.parse_args(argv)
    try:
        cfg = Config(base=args.base, markets=parse_markets(args.markets) if args.markets else Config.markets, max_hops=args.max_hops, size_min=args.size_min,
                     size_max=args.size_max, min_profit_bps=args.min_profit_bps, min_profit_base=args.min_profit).validate()
    except ValueError as e:
        raise SystemExit(f"bad settings: {e}")
    if args.window < cfg.max_hops + 1:
        raise SystemExit(f"--window needs at least {cfg.max_hops + 1} ledgers so a {cfg.max_hops}-leg trade can finish")
    from .feed import LiveFeed

    rpc = Rpc(tuple(args.rpc) if args.rpc else DEFAULT_RPC)
    feed = LiveFeed(cfg, rpc, log=lambda lvl, msg: None)
    now_idx, now_t = feed.latest()
    last = args.to_ledger or now_idx - int(args.ago_hours * LEDGERS_PER_HOUR) - 1
    first = args.from_ledger or last - int(args.hours * LEDGERS_PER_HOUR)
    starts = plan_windows(first, last, args.stride, args.window)
    if not starts:
        raise SystemExit("the range is shorter than one window")
    print(f"Reading {len(starts)} windows of {args.window} ledgers between ledger {first:,} and {last:,} (server {rpc.url}) ...", file=sys.stderr)
    t0 = time.time()

    def progress(n, total, skipped):
        if n % 5 == 0 or n == total:
            eta = (time.time() - t0) / n * (total - n)
            print(f"  {n}/{total} windows, {skipped} skipped, about {eta / 60:.1f} min left", file=sys.stderr, flush=True)

    samples, state = replay(cfg, feed, starts, args.window, args.workers, progress)
    feed.close()
    if not samples:
        raise SystemExit("no ledger could be read: is the server a full-history node?")
    first_t, last_t = min(s["t"] for s in samples), max(s["t"] for s in samples)
    rep = summarize(samples, state.stats, starts, first_t, last_t, args.stride, args.window)
    print_report(rep, cfg.base)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(rep, f, indent=1)
        print(f"\nfull report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
