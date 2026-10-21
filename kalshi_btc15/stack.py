"""The v2 stacked model of PREREGISTERED_v2.txt: the market's own price as the base, plus tests of what else adds to it.

One model, fixed in advance and fitted once on DEV; the HOLDOUT is scored once. Standard library only.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .api import Market
from .data import Sample
from .report import _row, SUMMARY_HEADERS, table, verdict
from .stats import Summary, norm_cdf, summarize
from .strategies import PREREGISTERED, Config, effective_minutes, fee_cents, run, side_price

NAMES = ["intercept", "market (logit mid)", "gap z-score", "market momentum (3 min)", "last trade vs mid", "BTC momentum (3 min, sigma units)"]
CLIP_P = 0.005
CLIP_Z = 6.0
RIDGE = 1.0
EDGE_CENTS = 2.0


def day_ts(text: str) -> int:
    return calendar.timegm(datetime.strptime(text, "%Y-%m-%d").timetuple())


DEV_RANGES = [(day_ts("2025-12-10"), day_ts("2026-01-04")), (day_ts("2026-08-21"), 1 << 62)]
HOLDOUT_RANGE = (day_ts("2026-01-04"), day_ts("2026-08-21"))
HOLDOUT_EVERY = 4


def in_dev(ts: int) -> bool:
    return any(lo <= ts < hi for lo, hi in DEV_RANGES)


def sample_range(markets: list[Market], lo: int, hi: int, every: int) -> list[Market]:
    """Every Nth market by close time inside [lo, hi): a deterministic sample that looks at no prices or outcomes."""
    return [m for m in sorted(markets, key=lambda x: x.close_ts) if lo <= m.close_ts < hi][::every]


def logit(p: float) -> float:
    p = min(max(p, CLIP_P), 1 - CLIP_P)
    return math.log(p / (1 - p))


def clip(x: float) -> float:
    return max(-CLIP_Z, min(CLIP_Z, x))


def features(s: Sample, cfg: Config) -> list[float] | None:
    """[market, gap z, market momentum, last-vs-mid, BTC momentum]; None when any input is missing."""
    if None in (s.spot_open, s.spot_entry, s.spot_prev, s.bid_prev, s.ask_prev) or not s.sigma:
        return None
    t_eff = effective_minutes(s.tau_min, cfg.settle_avg_min)
    if t_eff <= 0:
        return None
    market = logit(s.mid / 100)
    gap_z = clip(math.log(s.spot_entry / s.spot_open) / (s.sigma * math.sqrt(t_eff)))
    momentum = market - logit((s.bid_prev + s.ask_prev) / 200)
    last = (s.last - s.mid) / 100 if s.last is not None else 0.0
    btc = clip(math.log(s.spot_entry / s.spot_prev) / (s.sigma * math.sqrt(3)))
    return [market, gap_z, momentum, last, btc]


def invert(a: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan inverse with partial pivoting (the matrices here are at most 6 x 6)."""
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("singular matrix")
        m[col], m[piv] = m[piv], m[col]
        scale = m[col][col]
        m[col] = [v / scale for v in m[col]]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col]
                m[r] = [v - f * w for v, w in zip(m[r], m[col])]
    return [row[n:] for row in m]


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


def fit_logistic(x: list[list[float]], y: list[int], penalty: list[float]) -> tuple[list[float], list[float]]:
    """Ridge logistic regression by Newton / IRLS. Columns of x include the intercept; penalty is per coefficient.

    Returns (coefficients, standard errors from the inverse Hessian at the optimum).
    """
    k = len(x[0])
    beta = [0.0] * k
    for _ in range(60):
        grad = [-penalty[j] * beta[j] for j in range(k)]
        hess = [[penalty[i] if i == j else 0.0 for j in range(k)] for i in range(k)]
        for row, target in zip(x, y):
            p = sigmoid(sum(b * v for b, v in zip(beta, row)))
            w = max(p * (1 - p), 1e-9)
            for i in range(k):
                grad[i] += (target - p) * row[i]
                for j in range(i + 1):
                    hess[i][j] += w * row[i] * row[j]
        for i in range(k):
            for j in range(i + 1, k):
                hess[i][j] = hess[j][i]
        inv = invert(hess)
        step = [sum(inv[i][j] * grad[j] for j in range(k)) for i in range(k)]
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    return beta, [math.sqrt(max(inv[i][i], 0.0)) for i in range(k)]


@dataclass(frozen=True)
class StackModel:
    beta: list[float]       # intercept, market, then four standardised features
    se: list[float]
    mean: list[float]       # DEV mean / sd of the four non-market features, used to standardise at prediction time
    sd: list[float]
    cal_beta: list[float]   # calibration-only model: intercept and market

    def design(self, f: list[float]) -> list[float]:
        return [1.0, f[0]] + [(v - m) / s for v, m, s in zip(f[1:], self.mean, self.sd)]

    def p(self, f: list[float]) -> float:
        return sigmoid(sum(b * v for b, v in zip(self.beta, self.design(f))))

    def p_calibrated(self, f: list[float]) -> float:
        return sigmoid(self.cal_beta[0] + self.cal_beta[1] * f[0])


def fit_stack(dev: list[Sample], cfg: Config) -> tuple[StackModel, int]:
    rows = [(f, s.yes_won) for s in dev if (f := features(s, cfg)) is not None]
    n = len(rows)
    cols = list(zip(*[f[1:] for f, _ in rows]))
    mean = [sum(c) / n for c in cols]
    sd = [max(math.sqrt(sum((v - m) ** 2 for v in c) / n), 1e-9) for c, m in zip(cols, mean)]
    shell = StackModel([], [], mean, sd, [])
    y = [int(w) for _, w in rows]
    beta, se = fit_logistic([shell.design(f) for f, _ in rows], y, [0.0, 0.0] + [RIDGE] * 4)
    cal, _ = fit_logistic([[1.0, f[0]] for f, _ in rows], y, [0.0, 0.0])
    return StackModel(beta, se, mean, sd, cal), n


def make_strategy(model: StackModel, edge_cents: float = EDGE_CENTS) -> Callable[[Sample, Config], str | None]:
    """stack_edge: buy the side with the larger edge (100 * p - price - fee) if it is at least edge_cents."""
    def stack_edge(s: Sample, cfg: Config) -> str | None:
        f = features(s, cfg)
        if f is None:
            return None
        p = model.p(f)
        best_side, best = None, edge_cents
        for side, prob in (("yes", p), ("no", 1 - p)):
            price = side_price(s, side)
            if 0 < price < 100:
                edge = 100 * prob - price - fee_cents(price, cfg.contracts, cfg.fee_multiplier)
                if edge >= best:
                    best_side, best = side, edge
        return best_side
    return stack_edge


def log_loss(p: float, won: bool) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if won else 1 - p)


@dataclass(frozen=True)
class Comparison:
    name: str
    mean_loss: float
    brier: float
    delta: float | None = None   # mean log loss minus the raw mid-price's, paired per market (negative = better)
    se: float | None = None
    p_better: float | None = None


def compare(holdout: list[Sample], model: StackModel, cfg: Config) -> list[Comparison]:
    rows = [(s, f) for s in holdout if (f := features(s, cfg)) is not None]
    forecasts = {
        "raw mid-price": lambda s, f: min(max(s.mid / 100, 1e-6), 1 - 1e-6),
        "calibrated market (x1 only)": lambda s, f: model.p_calibrated(f),
        "stacked model": lambda s, f: model.p(f),
    }
    base = [log_loss(forecasts["raw mid-price"](s, f), s.yes_won) for s, f in rows]
    out = []
    for name, fn in forecasts.items():
        losses = [log_loss(fn(s, f), s.yes_won) for s, f in rows]
        brier = sum((fn(s, f) - s.yes_won) ** 2 for s, f in rows) / len(rows)
        if name == "raw mid-price":
            out.append(Comparison(name, sum(losses) / len(rows), brier))
            continue
        diffs = [a - b for a, b in zip(losses, base)]
        mean = sum(diffs) / len(diffs)
        se = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1) / len(diffs))
        out.append(Comparison(name, sum(losses) / len(rows), brier, mean, se, norm_cdf(mean / se) if se else 1.0))
    return out


@dataclass(frozen=True)
class StackResult:
    model: StackModel
    n_dev: int
    n_holdout: int
    comparisons: list[Comparison]
    dev_trades: Summary
    holdout_trades: Summary
    trades_verdict: str
    forecast_verdict: str


def evaluate(dev: list[Sample], holdout: list[Sample], cfg: Config) -> StackResult:
    model, n_dev = fit_stack(dev, cfg)
    strategy = make_strategy(model)
    dev_sum, hold_sum = summarize(run(strategy, dev, cfg)), summarize(run(strategy, holdout, cfg))
    comps = compare(holdout, model, cfg) if holdout else []
    stacked = comps[-1] if comps else None
    f_verdict = ("no holdout data" if stacked is None else
                 "PASS: the stack adds information to the mid-price" if stacked.p_better < 0.01 and stacked.delta < 0 else
                 "no information added")
    return StackResult(model, n_dev, sum(features(s, cfg) is not None for s in holdout), comps, dev_sum, hold_sum,
                       verdict(dev_sum, hold_sum), f_verdict)


def render_stack(r: StackResult, dev: list[Sample], holdout: list[Sample], cfg: Config, in_range: dict[str, int]) -> str:
    coef = [[NAMES[i], f"{r.model.beta[i]:+.3f}", f"{r.model.se[i]:.3f}", f"{r.model.beta[i] / r.model.se[i]:+.1f}"] for i in range(6)]
    parts = ["# Kalshi KXBTC15M v2: stacked model on a fresh holdout", "",
             f"DEV (fit, seen before): {r.n_dev} markets with all inputs. HOLDOUT (never seen): {r.n_holdout} markets with all inputs "
             f"({in_range['holdout_sampled']} sampled, {in_range['holdout_downloaded']} downloaded).", "",
             "## Fitted on DEV (b1 = 1 with every other row 0 would mean the market is already perfectly calibrated and complete)", "",
             table(["term", "coefficient", "std error", "z"], coef), "",
             "Features other than the market are standardised on DEV, so their coefficients are per one standard deviation. "
             "Ridge penalty 1.0 on those four.", ""]
    if r.comparisons:
        rows = [[c.name, f"{c.mean_loss:.4f}", f"{c.brier:.4f}",
                 "" if c.delta is None else f"{c.delta:+.5f} ± {c.se:.5f}", "" if c.p_better is None else f"{c.p_better:.4f}"]
                for c in r.comparisons]
        parts += ["## H_F: does the stack add information? (HOLDOUT, scored once)", "",
                  table(["forecast", "log loss", "Brier", "Δ log loss vs raw mid (paired)", "p(better)"], rows), "",
                  f"**Verdict H_F:** {r.forecast_verdict} (pre-registered: p < 0.01 against the raw mid-price).", ""]
        cuts = []
        for edge in (0, 1, 2, 3, 5, 8):
            t = run(make_strategy(r.model, float(edge)), holdout, cfg)
            cuts.append(_row(f"stack_edge ≥ {edge}¢" + ("  (pre-registered)" if edge == EDGE_CENTS else ""), summarize(t)))
        base = [_row("stack_edge in-sample (DEV)", r.dev_trades), _row("**stack_edge HOLDOUT**", r.holdout_trades)]
        parts += ["## H_T: does it make money after the spread and fee? (HOLDOUT)", "",
                  table(SUMMARY_HEADERS, base), "", f"**Verdict H_T:** {r.trades_verdict}", "",
                  "### Threshold sensitivity (exploratory, holdout)", "", table(SUMMARY_HEADERS, cuts), ""]
        v1 = [_row(name, summarize(run(strat, holdout, cfg))) for name, strat in PREREGISTERED.items()]
        parts += ["### The five v1 strategies on the same holdout (no verdict here; the final v1 verdict is its own run)", "",
                  table(SUMMARY_HEADERS, v1), ""]
        months: dict[str, list] = {}
        for t in run(make_strategy(r.model), holdout, cfg):
            months.setdefault(datetime.fromtimestamp(t.close_ts, timezone.utc).strftime("%Y-%m"), []).append(t)
        parts += ["### stack_edge by month (holdout)", "",
                  table(["month", "trades", "win rate", "mean P&L ¢"],
                        [[k, len(v), f"{100 * sum(t.win for t in v) / len(v):.1f}%", f"{sum(t.pnl for t in v) / len(v):+.2f}"]
                         for k, v in sorted(months.items())]), ""]
    else:
        parts += ["No holdout markets downloaded yet: only the DEV fit above is available.", ""]
    parts += ["## In-sample (DEV) trading, informational", "", table(SUMMARY_HEADERS, [_row("stack_edge (DEV)", r.dev_trades)]), ""]
    return "\n".join(parts)
