"""The trading policy, written once and driven by both executors (paper fills it from later ledgers, live from real transactions).

It is a generator: it yields the next payment to attempt and is sent back how it went.

  * first leg: must fill within `slip_bps` of its quote, otherwise the cycle is abandoned before any money has moved
  * middle legs: must fill within `max_loss_bps` of their quote
  * final leg: must return the invested amount plus the required profit; if the market has moved, one more attempt (the unwind)
    accepts any return within `max_loss_bps` of the invested amount
  * if even the unwind fails we are holding the intermediate asset: STUCK, and the bot halts for a person to look

No attempt is sent unless a simulation says it would succeed, so a failed attempt costs nothing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generator

from .config import Config
from .routes import Evaluation


@dataclass
class Attempt:
    leg: int  # index into the route's legs
    spend: float  # amount of the leg's source asset to convert
    min_out: float  # least acceptable amount of its destination asset
    kind: str  # "leg" | "final" | "unwind"


@dataclass
class Result:
    ok: bool
    out: float = 0.0  # amount received (only meaningful when ok)
    sent: bool = False  # a transaction was actually submitted (and its fee paid)
    note: str = ""  # why it failed, for the log


@dataclass
class Outcome:
    status: str  # WON | LOST | MISSED | STUCK
    realized: float  # gross, in base units, before fees
    note: str
    holding: tuple[int, float] | None = None  # STUCK: (index of the asset in the route, amount) still held


def threshold(cfg: Config, size: float) -> float:
    """Smallest net profit (base units) a trade of this size must show."""
    return max(cfg.min_profit_base, size * cfg.min_profit_bps / 1e4)


def run_plan(ev: Evaluation, cfg: Config) -> Generator[Attempt, Result, Outcome]:
    n = len(ev.legs)
    holding = ev.size
    for i, leg in enumerate(ev.legs[:-1]):
        tol = (cfg.slip_bps if i == 0 else cfg.max_loss_bps) / 1e4
        r = yield Attempt(i, holding, leg.amount_out * (1 - tol), "leg")
        if not r.ok:
            if i == 0:
                return Outcome("MISSED", 0.0, f"leg 1 {r.note or 'could not fill near its quote'}; nothing was spent")
            return Outcome("STUCK", -ev.size * cfg.max_loss_bps / 1e4, f"leg {i + 1} {r.note or 'failed'}", (i, holding))
        holding = r.out
    want = ev.size + threshold(cfg, ev.size)
    r = yield Attempt(n - 1, holding, want, "final")
    if r.ok:
        return Outcome("WON", r.out - ev.size, "cycle completed")
    r = yield Attempt(n - 1, holding, ev.size * (1 - cfg.max_loss_bps / 1e4), "unwind")
    if r.ok:
        res = r.out - ev.size
        return Outcome("WON" if res > 0 else "LOST", res, f"profit gone by the last leg; unwound at {res / ev.size:+.2%}")
    return Outcome("STUCK", -ev.size * cfg.max_loss_bps / 1e4, "last leg could not be sold within the loss limit", (n - 1, holding))
