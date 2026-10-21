"""Usage:
    python -m poly_mm fetch --hours 48          # download and cache the trade tape of every resolved 5-minute BTC market
    python -m poly_mm backtest --hours 48       # paper market maker over those tapes, train/test split

Read-only: public APIs only, no wallet, no key, no order code."""

from __future__ import annotations

import argparse
import itertools
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .markets import Market, load_market, window_starts
from .model import Quoter, Summary, simulate, summarize
from .tape import fetch_tape


def load_all(hours: float, workers: int) -> list[tuple[Market, list]]:
    starts = window_starts(int(time.time()), hours)

    def one(s):
        m = load_market(s)
        return (m, fetch_tape(m)) if m else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = [r for r in pool.map(one, starts) if r]
    print(f"{len(rows)} resolved markets with a tape (of {len(starts)} windows)")
    return rows


def fmt(name: str, s: Summary) -> str:
    pc = f"{s.pair_cost:.3f}" if s.pair_cost else "  -  "
    return f"{name:44s} {s.markets:5d} {s.pnl:+9.1f} {s.per_market:+7.2f} {s.margin_on_volume * 100:+6.2f}% {s.win_rate * 100:5.1f}% {pc:>6s} {s.imbalance:5.2f} {s.worst:+8.1f} {s.fills:6d}"


HEAD = f"{'strategy':44s} {'mkts':>5s} {'PnL $':>9s} {'$/mkt':>7s} {'margin':>7s} {'win%':>6s} {'pair$':>6s} {'imb':>5s} {'worst $':>8s} {'fills':>6s}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="poly_mm", description="Polymarket 5-minute BTC market-making research (read-only)")
    p.add_argument("cmd", choices=("fetch", "backtest"))
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    rows = load_all(args.hours, args.workers)
    if args.cmd == "fetch":
        print(f"tapes cached under data/poly/tape/ ({sum(len(t) for _, t in rows):,} trades)")
        return 0
    cut = len(rows) // 2
    train, test = rows[:cut], rows[cut:]
    print(f"train: first {len(train)} markets, test: last {len(test)} markets\n")
    base = Quoter()
    grid = [("tweet: static 0.48/0.48, whole window", replace(base, fair="static", refresh_s=1e9, stop_before_s=0, latency_s=0))]
    for delta, refresh, skew in itertools.product((0.02, 0.03, 0.05), (15, 30), (0.0, 0.0002, 0.0006)):
        grid.append((f"follow d={delta:.2f} refresh={refresh}s skew={skew}", replace(base, delta=delta, refresh_s=refresh, stop_before_s=60, skew=skew)))
    print("TRAIN (queue=0.3)\n" + HEAD)
    scored = []
    for name, q in grid:
        s = summarize([simulate(m, t, q) for m, t in train])
        scored.append((s.pnl, name, q))
        print(fmt(name, s))
    best = max(scored, key=lambda x: x[0])
    print(f"\nbest on train: {best[1]}\n\nTEST (never used to choose anything)\n" + HEAD)
    for name, q in [grid[0][:2], (best[1], best[2])]:
        for queue in (0.0, 0.3, 1.0):
            print(fmt(f"{name[:34]} queue={queue}", summarize([simulate(m, t, replace(q, queue=queue)) for m, t in test])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
