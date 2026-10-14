"""Auto-tuning with clones.

While the bot runs, a few CLONES of it trade the same tokens on paper, each with its settings nudged a little (a bit
higher take-profit here, a tighter stop there). After every ROUND of N launches the clones are compared with the real
bot launch by launch. A clone only counts as better if it beats the bot by more than luck would explain, and when
some do, the bot's settings move PART of the way toward them, weighted by how much each clone gained. Then a new
round starts around the new settings.

What keeps this honest:
  * Every clone is judged only on launches that arrived AFTER it was created, so a clone can't have been fitted to
    the data it is scored on.
  * The comparison is paired (same launch, clone result minus the bot's result) and uses a bootstrap lower bound.
    The required confidence is tightened for the number of clones, because the best of several tries looks good by luck.
  * A clone must differ from the bot on several launches; one lucky trade proves nothing.
  * Settings move only part of the way (`blend`), stay inside fixed limits, and never go outside what the settings screen accepts.

What it can NOT do: create an edge that isn't there. If no setting works, clones just find noise and (rarely) get
promoted by chance. After a repeat of data the bot has already seen the tuner pauses, because that would be memorising.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

from ..pump.curve import quote_sell
from ..pump.events import NewToken

CAUTION = {"eager": 0.80, "balanced": 0.90, "careful": 0.95, "strict": 0.99}
BOOTSTRAP_DRAWS = 1000
MIN_DIFFERING = 5       # a clone must act differently from the bot on at least this many launches in the round
MAX_HISTORY = 30
SEED = 20261014


def mulberry32(seed: int) -> Callable[[], float]:
    """The same small seeded generator the browser build uses, so both make identical 'random' choices."""
    mask = 0xFFFFFFFF
    a = seed & mask

    def rnd() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & mask
        t = ((a ^ (a >> 15)) * (1 | a)) & mask
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & mask)) & mask) ^ t
        return ((t ^ (t >> 14)) & mask) / 4294967296

    return rnd


def snap(x: float, precision: float) -> float:
    """Round to a multiple of `precision` (half up, with float dust removed) - the browser build does exactly the same."""
    v = math.floor(x / precision + 0.5) * precision
    return math.floor(v * 1e6 + 0.5) / 1e6


def pct(x: float) -> int:
    return math.floor(x * 100 + 0.5)


@dataclass(frozen=True)
class Tunable:
    field: str            # the Strategy attribute
    key: str              # the same setting's name on the settings screen / API
    label: str
    lo: float             # fixed limits (widened if the bot is already outside them)
    hi: float
    step: float           # size of a typical nudge: a fraction of the current value, or in the setting's own units when `absolute`
    precision: float      # values are rounded to a multiple of this
    absolute: bool = False
    integer: bool = False
    needs: Callable = lambda s: True  # when this setting matters at all for the current strategy


TUNABLES = (
    Tunable("take_profit_pct", "take_profit", "Take profit (%)", 8, 150, 0.2, 0.5, needs=lambda s: s.exit_mode == "smart" or (s.exit_mode == "targets" and not s.ladder)),
    Tunable("stop_loss_pct", "stop_loss", "Stop loss (%)", 5, 60, 0.2, 0.5),
    Tunable("trailing_stop_pct", "trailing_stop_pct", "Trailing stop (%)", 0, 40, 4, 0.5, absolute=True, needs=lambda s: s.exit_mode == "targets"),
    Tunable("max_hold_s", "max_hold_s", "Max hold (s)", 30, 1800, 0.3, 5),
    Tunable("min_liquidity_sol", "min_liquidity_sol", "Min liquidity (SOL)", 0, 20, 0.5, 0.1, absolute=True),
    Tunable("max_dev_buy_pct", "max_dev_buy_pct", "Max dev hold (%)", 1, 50, 0.25, 0.5),
    Tunable("min_trades", "min_trades", "Min trades seen", 0, 30, 2, 1, absolute=True, integer=True),
    Tunable("max_entry_gain_pct", "max_entry_gain_pct", "Max gain already (%)", 10, 1000, 0.25, 1, needs=lambda s: s.max_entry_gain_pct < 100_000),
    Tunable("confirm_gain_pct", "confirm_gain_pct", "Confirmation gain (%)", 1, 60, 0.25, 0.5, needs=lambda s: s.entry_mode == "confirm"),
)
BY_KEY = {t.key: t for t in TUNABLES}
DEFAULT_PARAMS = tuple(t.key for t in TUNABLES)


@dataclass
class TunerConfig:
    enabled: bool = False
    clones: int = 6
    round_launches: int = 100
    caution: str = "balanced"
    blend: float = 0.5
    params: tuple[str, ...] = DEFAULT_PARAMS


def parse_tuner(data: dict, current: TunerConfig) -> TunerConfig:
    """Validate a (possibly partial) tuner settings payload."""
    unknown = set(data) - {"enabled", "clones", "round_launches", "caution", "blend", "params"}
    if unknown:
        raise ValueError(f"unknown tuner setting: {sorted(unknown)[0]}")
    enabled = data.get("enabled", current.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("Auto-tune must be on or off")

    def whole(key, label, lo, hi, cur):
        v = data.get(key, cur)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")) or v != int(v) or not lo <= v <= hi:
            raise ValueError(f"{label} must be a whole number from {lo} to {hi}")
        return int(v)

    clones = whole("clones", "Number of clones", 2, 8, current.clones)
    rounds = whole("round_launches", "Launches per round", 20, 1000, current.round_launches)
    caution = data.get("caution", current.caution)
    if not isinstance(caution, str) or caution not in CAUTION:
        raise ValueError(f"Caution must be one of {list(CAUTION)}")
    blend = data.get("blend", current.blend)
    if isinstance(blend, bool) or not isinstance(blend, (int, float)) or blend != blend or not 0.1 <= blend <= 1.0:
        raise ValueError("Step size must be a number from 0.1 to 1")
    params = data.get("params", list(current.params))
    if not isinstance(params, list) or any(not isinstance(p, str) or p not in BY_KEY for p in params) or len(set(params)) != len(params):
        raise ValueError(f"params must be a list drawn from {list(BY_KEY)}")
    return TunerConfig(enabled, clones, rounds, caution, float(blend), tuple(k for k in DEFAULT_PARAMS if k in params))


@dataclass
class Clone:
    id: int
    engine: object
    changes: dict  # settings key -> the value this clone tries


def launch_outcomes(engine, mints: list[str]) -> dict[str, float]:
    """Net return (%) of what `engine` did on each launch: closed trades, plus open positions marked to the current price."""
    wanted = set(mints)
    out: dict[str, float] = {}
    for c in engine.closed:
        if c.mint in wanted:
            out[c.mint] = out.get(c.mint, 0.0) + c.ret_pct
    for mint, pos in engine.positions.items():
        if mint in wanted and mint in engine.curve:
            total = pos.realized_pnl + quote_sell(engine.curve[mint], pos.tokens, engine.s.fee_bps) - engine.s.tx_cost_sol - pos.sol_in - pos.entry_tx_left
            out[mint] = out.get(mint, 0.0) + total / pos.orig_sol_in * 100
    return out


def trade_count(engine, mints: list[str]) -> int:
    wanted = set(mints)
    return len({c.mint for c in engine.closed if c.mint in wanted} | {m for m in engine.positions if m in wanted})


class Tuner:
    def __init__(self, config: TunerConfig | None = None, seed: int = SEED, gate: Callable | None = None, blender: Callable | None = None):
        self._gate, self._blender = gate, blender  # replaceable so tests can script the statistics
        self.cfg = config or TunerConfig()
        self.rng = mulberry32(seed)
        self.clones: list[Clone] = []
        self.round_mints: list[str] = []
        self.round_no = 0
        self.history: list[dict] = []
        self.paused: str | None = None

    # ---- control ----
    def configure(self, data: dict) -> TunerConfig:
        new = parse_tuner(data, self.cfg)
        self.cfg = new
        self.clear()
        return new

    def clear(self) -> None:
        """Drop the running clones; the next event starts a fresh round around the bot's current settings."""
        self.clones, self.round_mints = [], []

    # ---- the stream ----
    def feed(self, ev, champion) -> dict | None:
        """Give the tuner an event the real bot (`champion`) has already processed. Returns a verdict when a round ends."""
        if not self.cfg.enabled or self.paused:
            return None
        if not self.clones:
            self._start_round(champion)
            return None
        for c in self.clones:
            c.engine.on_event(ev)
        if isinstance(ev, NewToken):
            self.round_mints.append(ev.mint)
            if len(self.round_mints) >= self.cfg.round_launches:
                verdict = self._judge(champion, ev.t)
                self.clear()
                return verdict
        return None

    # ---- rounds ----
    def active_tunables(self, strategy) -> list[Tunable]:
        return [t for t in TUNABLES if t.key in self.cfg.params and t.needs(strategy)]

    def _start_round(self, champion) -> None:
        self.round_mints = []
        tunables = self.active_tunables(champion.s)
        plans = self._plan(champion.s, tunables)
        self.clones = []
        for i, changes in enumerate(plans):
            strat = replace(champion.s, **{BY_KEY[k].field: (int(v) if BY_KEY[k].integer else v) for k, v in changes.items()}) if changes else champion.s
            self.clones.append(Clone(i + 1, champion.fork(strat), changes))

    def _plan(self, strategy, tunables: list[Tunable]) -> list[dict]:
        """Settings for each clone: nudges come in opposite pairs (+d / -d on the same settings) so luck cancels out."""
        n = self.cfg.clones
        plans: list[dict] = []
        if not tunables:
            return [{} for _ in range(n)]
        for _ in range((n + 1) // 2):
            chosen = [t for t in tunables if self.rng() < 0.5]
            if not chosen:
                chosen = [tunables[int(self.rng() * len(tunables))]]
            up: dict = {}
            down: dict = {}
            for t in chosen:
                cur = float(getattr(strategy, t.field))
                size = max(t.step if t.absolute else t.step * abs(cur), t.precision)
                size *= 0.5 + 0.5 * self.rng()
                lo, hi = min(t.lo, cur), max(t.hi, cur)
                a = min(max(snap(cur + size, t.precision), lo), hi)
                b = min(max(snap(cur - size, t.precision), lo), hi)
                if a != cur:
                    up[t.key] = a
                if b != cur:
                    down[t.key] = b
            plans.append(up)
            plans.append(down)
        return plans[:n]

    def _judge(self, champion, now: int) -> dict:
        mints = self.round_mints
        n = len(mints)
        self.round_no += 1  # rounds are numbered by how many have finished, so an abandoned round leaves no gap
        bot_vec = out_vec(launch_outcomes(champion, mints), mints)
        diffs = []
        for c in self.clones:
            out = launch_outcomes(c.engine, mints)
            diffs.append([out.get(m, 0.0) - bot_vec[i] for i, m in enumerate(mints)])
        gate = (self._gate or paired_gate)(diffs, alpha_for(self.cfg.caution, len(self.clones)), self.rng)
        rows = []
        for c, (mean, lcb, differing) in zip(self.clones, gate):
            rows.append({"id": c.id, "changes": dict(c.changes), "trades": trade_count(c.engine, mints), "mean": mean, "lcb": lcb,
                         "differing": differing, "passed": clone_passes(c.changes, mean, lcb, differing)})
        winners = [(c.changes, r["mean"]) for c, r in zip(self.clones, rows) if r["passed"]]
        changes = (self._blender or blend_changes)(champion.s, self.active_tunables(champion.s), winners, self.cfg.blend)
        confidence = pct(CAUTION[self.cfg.caution])
        if winners and changes:
            decision, reason = "changed", f"{len(winners)} of {len(self.clones)} clones beat the bot with {confidence}% confidence; moved {pct(self.cfg.blend)}% of the way toward them"
        elif winners:
            decision, reason = "kept", "the winning move was too small to apply"
        else:
            decision, reason = "kept", f"no clone beat the bot with {confidence}% confidence"
        verdict = {"round": self.round_no, "t": now, "launches": n, "bot_mean": _mean(bot_vec), "bot_trades": trade_count(champion, mints),
                   "clones": rows, "decision": decision, "reason": reason, "changes": changes}
        self.history.insert(0, verdict)
        del self.history[MAX_HISTORY:]
        return verdict

    # ---- views ----
    def snapshot(self, champion) -> dict:
        cfg = self.cfg
        mints = self.round_mints
        bot_vec = out_vec(launch_outcomes(champion, mints), mints) if mints else []
        live = []
        for c in self.clones:
            row = {"id": c.id, "changes": dict(c.changes), "trades": 0, "mean": 0.0, "delta": 0.0}
            if mints:
                vec = out_vec(launch_outcomes(c.engine, mints), mints)
                row.update(trades=trade_count(c.engine, mints), mean=_mean(vec), delta=_mean([x - y for x, y in zip(vec, bot_vec)]))
            live.append(row)
        active = {t.key for t in TUNABLES if t.needs(champion.s)}
        return {
            "enabled": cfg.enabled, "paused": self.paused,
            "config": {"clones": cfg.clones, "round_launches": cfg.round_launches, "caution": cfg.caution, "blend": cfg.blend, "params": list(cfg.params)},
            "cautions": {k: pct(v) for k, v in CAUTION.items()},
            "round": self.round_no + 1 if self.clones else self.round_no, "progress": len(mints),
            "params": [{"key": t.key, "label": t.label, "tuned": t.key in cfg.params, "active": t.key in active, "lo": t.lo, "hi": t.hi,
                        "value": float(getattr(champion.s, t.field))} for t in TUNABLES],
            "clones": live, "bot_mean": _mean(bot_vec), "bot_trades": trade_count(champion, mints) if mints else 0,
            "history": self.history,
        }


def resample_counts(n: int, rng: Callable[[], float]) -> list[list[int]]:
    """`BOOTSTRAP_DRAWS` resamples of n launches, as how many times each launch was picked."""
    draws = []
    for _ in range(BOOTSTRAP_DRAWS):
        counts = [0] * n
        for _ in range(n):
            counts[math.floor(rng() * n)] += 1
        draws.append(counts)
    return draws


def alpha_for(caution: str, n_clones: int) -> float:
    """Allowed chance of promoting a clone by luck, split across the clones (the best of several tries looks good by chance)."""
    return (1 - CAUTION[caution]) / n_clones


def clone_passes(changes: dict, mean: float, lcb: float, differing: int) -> bool:
    """A clone earns a say only if it tried something, gained on average, acted differently on enough launches, and its lower bound is above zero."""
    return bool(changes) and mean > 0 and differing >= MIN_DIFFERING and lcb > 0


def paired_gate(diffs: list[list[float]], alpha: float, rng: Callable[[], float], counts: list[list[int]] | None = None) -> list[tuple[float, float, int]]:
    """For each clone's per-launch differences (clone minus bot): (mean, lower confidence bound, launches where it differed).

    The bound is the `alpha` quantile of the mean over `BOOTSTRAP_DRAWS` resamples of the launches (the same resamples for every clone).
    Launches where the clone did exactly what the bot did contribute nothing, so only the others are summed."""
    n = len(diffs[0]) if diffs else 0
    if n == 0:
        return [(0.0, 0.0, 0) for _ in diffs]
    draws = counts if counts is not None else resample_counts(n, rng)
    out = []
    for d in diffs:
        moved = [(j, x) for j, x in enumerate(d) if abs(x) > 1e-9]
        boots = []
        for c in draws:
            total = 0.0
            for j, x in moved:
                total += c[j] * x
            boots.append(total / n)
        boots.sort()
        out.append((_mean(d), boots[math.floor(alpha * len(draws))], len(moved)))
    return out


def blend_changes(strategy, tunables: list[Tunable], winners: list[tuple[dict, float]], blend: float) -> dict:
    """New values for the bot: move `blend` of the way toward the average of the winning clones, weighted by how much each gained.
    Returns {key: [old, new]} for the settings that actually change."""
    changes: dict = {}
    if not winners:
        return changes
    total = 0.0
    for _, gain in winners:
        total += gain
    for t in tunables:
        cur = float(getattr(strategy, t.field))
        avg = 0.0
        for tried, gain in winners:
            avg += gain / total * tried.get(t.key, cur)
        lo, hi = min(t.lo, cur), max(t.hi, cur)
        new = min(max(snap(cur + blend * (avg - cur), t.precision), lo), hi)
        if new != cur:
            changes[t.key] = [cur, new]
    return changes


def out_vec(out: dict, mints: list[str]) -> list[float]:
    return [out.get(m, 0.0) for m in mints]


def _mean(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = 0.0
    for x in xs:
        s += x
    return s / len(xs)
