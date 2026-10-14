"""Usage:
    python -m sniper.paper replay events.jsonl [strategy options]
    python -m sniper.paper sweep  events.jsonl [strategy options]
    python -m sniper.paper compare events.jsonl [--model model.json] [--tx-cost X]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from itertools import product

from ..pump.events import Event, NewToken, read_jsonl
from .engine import Strategy, replay
from .stats import MIN_TRADES_FOR_CI, Stats, summarise

TP_GRID = [15, 20, 30, 50, 100]
SL_GRID = [10, 20, 30, 50]


def split_events(events: list[Event], train_frac: float = 0.5) -> tuple[list[Event], list[Event]]:
    """Split by launch time so a token's whole history lands on one side."""
    launches = sorted((e for e in events if isinstance(e, NewToken)), key=lambda e: e.t)
    cut = int(len(launches) * train_frac)
    train = {e.mint for e in launches[:cut]}
    test = {e.mint for e in launches[cut:]}
    return [e for e in events if e.mint in train], [e for e in events if e.mint in test]


def baseline(s: Strategy) -> Strategy:
    """Same exits and costs, but buys every launch with no scanner or dev-buy filter."""
    return replace(s, require_scan=False, max_dev_buy_pct=100.0)


def fmt(label: str, st: Stats, skipped=None) -> str:
    ci = f"  95% CI [{st.expectancy_ci95[0]:+.1f}, {st.expectancy_ci95[1]:+.1f}]" if st.expectancy_ci95 else (
        f"  (CI needs {MIN_TRADES_FOR_CI}+ trades)")
    lines = [
        f"{label}",
        f"  trades {st.trades}   win rate {st.win_rate:.0f}%   avg win {st.avg_win_pct:+.1f}%   avg loss {st.avg_loss_pct:+.1f}%",
        f"  expectancy {st.expectancy_pct:+.2f}% per trade{ci}",
        f"  bankroll {st.final_equity:.3f} SOL ({st.return_pct:+.1f}%)   max drawdown {st.max_drawdown_pct:.1f}%",
        f"  exits {dict(sorted(st.exits.items()))}",
    ]
    if skipped:
        lines.append(f"  skipped {dict(sorted(skipped.items()))}")
    return "\n".join(lines)


def cmd_replay(events: list[Event], s: Strategy, start_sol: float) -> None:
    for label, strat in (("FILTERED (scanner + dev-buy rules)", s), ("BASELINE (buy every launch)", baseline(s))):
        r = replay(events, strat, start_sol)
        print(fmt(label, summarise(r), r.skipped) + "\n")
    print("Paper results ignore front-running, dropped transactions and post-graduation exits. Real results will be worse.")


def cmd_sweep(events: list[Event], s: Strategy, start_sol: float, train_frac: float) -> None:
    train, test = split_events(events, train_frac)
    rows = []
    for tp, sl in product(TP_GRID, SL_GRID):
        strat = replace(s, take_profit_pct=tp, stop_loss_pct=sl)
        st = summarise(replay(train, strat, start_sol))
        rows.append((tp, sl, st))
    # Settings with too few trades can't be ranked on luck, so they sort last.
    rows.sort(key=lambda r: (r[2].trades >= MIN_TRADES_FOR_CI, r[2].expectancy_pct), reverse=True)

    print(f"Train half: {len(train)} events.  Test half: {len(test)} events.\n")
    print("TRAIN (used to pick settings)        TEST (never seen when picking)")
    print(f"{'TP':>4} {'SL':>4} | {'n':>4} {'exp%':>7} | {'n':>4} {'exp%':>7} {'bankroll':>9}")
    for tp, sl, st in rows[:6]:
        te = summarise(replay(test, replace(s, take_profit_pct=tp, stop_loss_pct=sl), start_sol))
        print(f"{tp:>4} {sl:>4} | {st.trades:>4} {st.expectancy_pct:>+7.2f} | {te.trades:>4} {te.expectancy_pct:>+7.2f} {te.final_equity:>9.3f}")
    print("\nOnly trust a setting if the TEST column is also positive with a healthy trade count.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sniper.paper", description="Paper-trade recorded pump.fun events")
    p.add_argument("command", choices=["replay", "sweep", "compare"])
    p.add_argument("events", help="JSONL file written by sniper.listener")
    d = Strategy()
    p.add_argument("--tp", type=float, default=d.take_profit_pct, help="take profit %% (net of costs)")
    p.add_argument("--sl", type=float, default=d.stop_loss_pct, help="stop loss %%")
    p.add_argument("--hold", type=float, default=d.max_hold_s, help="max hold seconds")
    p.add_argument("--size", type=float, default=d.size_sol, help="SOL per trade")
    p.add_argument("--size-frac", type=float, default=None, help="fraction of equity per trade (compounding)")
    p.add_argument("--max-open", type=int, default=d.max_open)
    p.add_argument("--latency-ms", type=int, default=d.latency_ms)
    p.add_argument("--fee-bps", type=float, default=d.fee_bps)
    p.add_argument("--tx-cost", type=float, default=d.tx_cost_sol, help="SOL per transaction (priority fee + tip)")
    p.add_argument("--verdicts", default="PASS,CAUTION", help="comma list of scanner verdicts to buy")
    p.add_argument("--max-dev-buy", type=float, default=d.max_dev_buy_pct)
    p.add_argument("--max-entry-mcap", type=float, default=d.max_entry_mcap_sol)
    p.add_argument("--start-sol", type=float, default=1.0)
    p.add_argument("--train-frac", type=float, default=0.5)
    p.add_argument("--model", help="trained hold/sell model JSON (compare only)")
    p.add_argument("--sell-margin", type=float, default=0.10, help="model: sell when p_down - p_up reaches this")
    a = p.parse_args(argv)

    s = Strategy(
        take_profit_pct=a.tp, stop_loss_pct=a.sl, max_hold_s=a.hold, size_sol=a.size, size_frac=a.size_frac,
        max_open=a.max_open, latency_ms=a.latency_ms, fee_bps=a.fee_bps, tx_cost_sol=a.tx_cost,
        allowed_verdicts=frozenset(v.strip().upper() for v in a.verdicts.split(",")),
        max_dev_buy_pct=a.max_dev_buy, max_entry_mcap_sol=a.max_entry_mcap,
    )
    try:
        events = read_jsonl(a.events)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not any(isinstance(e, NewToken) for e in events):
        print("error: no launches in that file", file=sys.stderr)
        return 2

    if a.command == "compare":
        from ..policy.model import HoldModel
        from .compare import compare
        print(compare(events, HoldModel.load(a.model) if a.model else None, a.tx_cost, a.latency_ms, a.sell_margin))
        return 0
    if a.command == "replay":
        cmd_replay(events, s, a.start_sol)
    else:
        cmd_sweep(events, s, a.start_sol, a.train_frac)
    return 0


if __name__ == "__main__":
    sys.exit(main())
