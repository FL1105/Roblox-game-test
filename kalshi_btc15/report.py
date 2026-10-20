"""Turn samples into the Markdown report. The verdict rule is the one in PREREGISTERED.txt, applied mechanically."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import replace

from .data import Sample
from .stats import Summary, summarize, wilson
from .strategies import CONTROLS, PREREGISTERED, Config, fee_cents, model_edge, model_p_up, run, side_price

ALPHA = 0.05 / len(PREREGISTERED)
MIN_OOS_TRADES = 300
IN_SAMPLE_FRACTION = 0.6


def table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def split_ts(samples: list[Sample]) -> int:
    """Close time separating the first 60% of markets (in-sample) from the rest (out-of-sample)."""
    ts = sorted(s.close_ts for s in samples)
    return ts[int(len(ts) * IN_SAMPLE_FRACTION)] if ts else 0


def verdict(ins: Summary, oos: Summary) -> str:
    if oos.n < MIN_OOS_TRADES:
        return f"no verdict: {oos.n} out-of-sample trades, need {MIN_OOS_TRADES}"
    if oos.mean > 0 and oos.p_pos < ALPHA and ins.mean > 0:
        return "EDGE: meets all three pre-registered criteria"
    return "no edge shown"


def _row(label: str, s: Summary) -> list:
    return [label, s.n, f"{100 * s.win_rate:.1f}%", f"{s.avg_cost:.1f}", f"{s.mean:+.2f}",
            f"[{s.lo:+.2f}, {s.hi:+.2f}]", f"{s.p_pos:.3f}", f"{100 * s.roi:+.2f}%"]


SUMMARY_HEADERS = ["strategy / split", "trades", "win rate", "avg cost ¢", "mean P&L ¢/contract", "95% CI", "p(mean>0)", "ROI"]


def data_quality(samples: list[Sample], skipped: Counter, total_markets: int) -> str:
    with_spot = [s for s in samples if s.spot_open is not None and s.spot_end is not None]
    lines = [f"- Settled markets in range: {total_markets}", f"- Usable at entry: {len(samples)}"]
    lines += [f"- Dropped or lacking data, {why}: {n}" for why, n in sorted(skipped.items())]
    if with_spot:
        agree = sum((s.spot_end >= s.spot_open) == s.yes_won for s in with_spot)
        lines.append(f"- Coinbase direction agrees with Kalshi's recorded result: {100 * agree / len(with_spot):.1f}% of "
                     f"{len(with_spot)} (far below ~97% means a time-alignment problem, not a market finding)")
    offsets = [abs(s.strike - s.spot_open) / s.strike * 1e4 for s in with_spot if s.strike]
    if offsets:
        lines.append(f"- Median |Kalshi strike - Coinbase at window start|: {statistics.median(offsets):.1f} bps "
                     f"(the exchange-versus-index offset; trend is measured against Coinbase's own start price)")
    if samples:
        lines.append(f"- Close times: {min(s.close_ts for s in samples)} to {max(s.close_ts for s in samples)} (unix)")
    return "\n".join(lines)


def preregistered(samples: list[Sample], cfg: Config) -> tuple[str, dict[str, str]]:
    cut = split_ts(samples)
    rows, verdicts, risk = [], {}, []
    for name, strat in PREREGISTERED.items():
        trades = run(strat, samples, cfg)
        ins = summarize([t for t in trades if t.close_ts < cut])
        oos = summarize([t for t in trades if t.close_ts >= cut])
        both = summarize(trades)
        rows += [_row(f"**{name}** in-sample", ins), _row(f"**{name}** out-of-sample", oos), _row(f"{name} all", both)]
        verdicts[name] = verdict(ins, oos)
        risk.append([name, both.n, f"{both.avg_win:+.1f}", f"{both.avg_loss:+.1f}",
                     f"{abs(both.avg_loss) / both.avg_win:.1f}" if both.avg_win else "n/a",
                     f"{100 * both.avg_cost / 100:.1f}%", f"{100 * both.win_rate:.1f}%", both.losing_streak, f"{both.drawdown:.0f}"])
    out = [table(SUMMARY_HEADERS, rows), "", "**Verdicts** (rule in PREREGISTERED.txt, alpha = %.3f):" % ALPHA, ""]
    out += [f"- `{name}`: {v}" for name, v in verdicts.items()]
    out += ["", "**What the pattern feels like** (all trades; a win pays the left column, a loss costs the right one):", "",
            table(["strategy", "trades", "avg win ¢", "avg loss ¢", "wins to repay 1 loss", "break-even win rate", "actual win rate",
                   "longest losing streak", "max drawdown ¢/contract"], risk)]
    return "\n".join(out), verdicts


def controls(samples: list[Sample], cfg: Config) -> str:
    rows = []
    for name, strat in CONTROLS.items():
        rows.append(_row(name, summarize(run(strat, samples, cfg))))
    return table(SUMMARY_HEADERS, rows)


def buckets(samples: list[Sample], cfg: Config, width: int = 5) -> str:
    """Every ask a taker could have paid at entry (both sides), grouped by price: is the cheap end overpriced?"""
    groups: dict[int, list[tuple[float, bool]]] = {}
    for s in samples:
        for side in ("yes", "no"):
            price = side_price(s, side)
            if 0 < price < 100:
                win = s.yes_won == (side == "yes")
                groups.setdefault(int(price // width), []).append((price, win))
    rows = []
    for b in sorted(groups):
        obs = groups[b]
        n, wins = len(obs), sum(w for _, w in obs)
        avg_price = sum(p for p, _ in obs) / n
        fee = fee_cents(avg_price, cfg.contracts, cfg.fee_multiplier)
        lo, hi = wilson(wins, n)
        rows.append([f"{b * width}-{b * width + width}¢", n, f"{avg_price:.1f}", f"{100 * wins / n:.1f}%",
                     f"[{100 * lo:.1f}%, {100 * hi:.1f}%]", f"{100 * wins / n - avg_price - fee:+.2f}"])
    return table(["price paid", "observations", "avg price ¢", "win rate", "95% CI", "EV after fee ¢"], rows)


def threshold_sweep(samples: list[Sample], cfg: Config) -> str:
    cut = split_ts(samples)
    rows = []
    for edge in (0, 1, 2, 3, 5, 8):
        trades = run(model_edge, samples, replace(cfg, edge_cents=float(edge)))
        a, o = summarize(trades), summarize([t for t in trades if t.close_ts >= cut])
        rows.append([f"{edge}¢", a.n, f"{100 * a.win_rate:.1f}%", f"{a.mean:+.2f}", f"{a.p_pos:.3f}", o.n, f"{o.mean:+.2f}"])
    return table(["min edge", "trades", "win rate", "mean P&L ¢", "p(mean>0)", "OOS trades", "OOS mean ¢"], rows)


def lead_sweep(by_lead: dict[int, list[Sample]], cfg: Config) -> str:
    rows = []
    for lead, samples in sorted(by_lead.items(), reverse=True):
        row = [f"{lead} s left ({(900 - lead) // 60}:{(900 - lead) % 60:02d})"]
        for strat in PREREGISTERED.values():
            s = summarize(run(strat, samples, cfg))
            row.append(f"{s.mean:+.2f} (n={s.n})")
        rows.append(row)
    return table(["entry"] + list(PREREGISTERED), rows)


def brier(samples: list[Sample], cfg: Config) -> str:
    usable = [(s, p) for s in samples if (p := model_p_up(s, cfg)) is not None]
    if not usable:
        return "No samples with BTC data."
    n = len(usable)
    score = lambda f: sum((f(s, p) - s.yes_won) ** 2 for s, p in usable) / n
    rows = [["model (gap / volatility)", f"{score(lambda s, p: p):.4f}"],
            ["market mid-price", f"{score(lambda s, p: s.mid / 100):.4f}"],
            ["50/50 blend", f"{score(lambda s, p: (p + s.mid / 100) / 2):.4f}"]]
    return f"{n} markets; lower is better; always guessing 50% scores 0.2500.\n\n" + table(["forecast", "Brier score"], rows)


def render(samples: list[Sample], skipped: Counter, by_lead: dict[int, list[Sample]], cfg: Config,
           total_markets: int, title: str) -> str:
    pre, _ = preregistered(samples, cfg)
    parts = [
        f"# {title}", "",
        f"Entry: {int(samples[0].tau_min * 60) if samples else 120} s before close. Order: {cfg.contracts} contracts taker, fee x{cfg.fee_multiplier:g}. "
        "Fills assume the quoted best price; see KNOWN LIMITATIONS in PREREGISTERED.txt.", "",
        "## Data", "", data_quality(samples, skipped, total_markets), "",
        "## Pre-registered strategies (these get verdicts)", "", pre, "",
        "## Controls (no verdict)", "", controls(samples, cfg), "",
        "## Exploratory (no verdicts; cannot rescue a failed strategy)", "",
        "### Price buckets: win rate against the price paid", "", buckets(samples, cfg), "",
        "Each market contributes two observations (its YES and NO ask), which are mirror images, so the counts are doubled "
        "and the rows are not independent.", "",
        "### model_edge threshold", "", threshold_sweep(samples, cfg), "",
        "### Entry time (mean P&L ¢ per contract, all markets)", "", lead_sweep(by_lead, cfg), "",
        "### Probability forecast quality", "", brier(samples, cfg), "",
    ]
    return "\n".join(parts)
