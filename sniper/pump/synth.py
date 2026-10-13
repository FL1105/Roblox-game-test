"""Synthetic pump.fun market, for demos and tests only.

Everything here is INVENTED: the mix of token outcomes, how often the "scanner" is right,
the trade sizes. Results produced from this data say nothing about real markets or about
how well the real scanner works.
"""

from __future__ import annotations

import random

from .curve import CurveState, quote_buy
from .events import Event, NewToken, ScanNote, Trade

FEE_BPS = 100


class Sim:
    """Builds internally consistent events: reserves follow the real constant-product curve."""

    def __init__(self, mint: str = "MINT1", creator: str = "CREATOR", t: int = 1_000):
        self.mint, self.creator, self.t = mint, creator, t
        self.state = CurveState(30.0, 1_073_000_000.0)

    def create(self, dev_sol: float = 0.0) -> NewToken:
        tokens = self._buy(dev_sol) if dev_sol else 0.0
        return NewToken(self.t, self.mint, self.creator, "name", "SYM", tokens, dev_sol, self.state.v_sol, self.state.v_tokens)

    def scan(self, t: int, verdict: str = "PASS") -> ScanNote:
        return ScanNote(t, self.mint, verdict, 0, "")

    def buy(self, t: int, sol: float, trader: str = "BUYER") -> Trade:
        tokens = self._buy(sol)
        return Trade(t, self.mint, trader, "buy", sol, tokens, self.state.v_sol, self.state.v_tokens)

    def sell(self, t: int, tokens: float, trader: str) -> Trade:
        s = self.state
        gross = s.v_sol - s.v_sol * s.v_tokens / (s.v_tokens + tokens)
        self.state = CurveState(s.v_sol - gross, s.v_tokens + tokens)
        return Trade(t, self.mint, trader, "sell", gross * (1 - FEE_BPS / 10_000), tokens, self.state.v_sol, self.state.v_tokens)

    def _buy(self, sol: float) -> float:
        tokens = quote_buy(self.state, sol, FEE_BPS)
        self.state = CurveState(self.state.v_sol + sol * (1 - FEE_BPS / 10_000), self.state.v_tokens - tokens)
        return tokens


_ARCHETYPES = [("dead", 0.62), ("rug", 0.18), ("pump", 0.13), ("runner", 0.05), ("grad", 0.02)]
_VERDICTS_RUG = [("REJECT", 0.25), ("AVOID", 0.30), ("CAUTION", 0.25), ("PASS", 0.20)]
_VERDICTS_OTHER = [("PASS", 0.55), ("CAUTION", 0.30), ("AVOID", 0.10), ("REJECT", 0.05)]
_NAMES = ["Moon", "Frog", "Doge", "Pepe", "Cat", "Rocket", "Bonk", "Wif", "Gigachad", "Pump", "Based", "Ape"]


def _pick(rng: random.Random, table: list[tuple[str, float]]) -> str:
    r, acc = rng.random(), 0.0
    for name, p in table:
        acc += p
        if r < acc:
            return name
    return table[-1][0]


def _token(rng: random.Random, idx: int, t0: int) -> list[Event]:
    kind = _pick(rng, _ARCHETYPES)
    mint = f"DEMO{idx:05d}{rng.getrandbits(40):010x}"
    creator = f"DEMOCREATOR{rng.randrange(60):02d}"
    sim = Sim(mint, creator, t0)
    dev_sol = rng.uniform(1.0, 4.0) if kind == "rug" else rng.choice([0, 0, 0.5, 1.0, 2.0, 3.0])
    launch = sim.create(dev_sol)
    launch = NewToken(**{**launch.__dict__, "name": f"{rng.choice(_NAMES)} {idx}", "symbol": f"{rng.choice(_NAMES)[:4].upper()}{idx % 100}"})
    verdict = _pick(rng, _VERDICTS_RUG if kind == "rug" else _VERDICTS_OTHER)
    events: list[Event] = [launch, sim.scan(t0 + rng.randint(600, 2500), verdict)]
    others = [0.0]  # tokens held by non-creators, so sells never exceed what was bought

    def buy(at_s: float, sol: float) -> None:
        tr = sim.buy(t0 + int(at_s * 1000), sol, f"B{rng.getrandbits(32):08x}")
        others[0] += tr.tokens
        events.append(tr)

    def sell_others(at_s: float, frac: float) -> None:
        tokens = others[0] * frac
        if tokens > 0:
            others[0] -= tokens
            events.append(sim.sell(t0 + int(at_s * 1000), tokens, f"S{rng.getrandbits(32):08x}"))

    if kind == "dead":
        for _ in range(rng.randint(4, 12)):
            at = rng.uniform(3, 60)
            buy(at, rng.uniform(0.05, 0.8)) if rng.random() < 0.65 else sell_others(at, rng.uniform(0.2, 0.5))
    elif kind in ("pump", "runner"):
        lo, hi, n = (0.6, 4.0, rng.randint(8, 16)) if kind == "pump" else (1.5, 6.0, rng.randint(14, 24))
        span = 120 if kind == "pump" else 200
        for _ in range(n):
            buy(rng.uniform(10, span), rng.uniform(lo, hi))
        for k in range(3):
            sell_others(span + 30 + k * 20 + rng.uniform(0, 10), rng.uniform(0.1, 0.25))
    elif kind == "rug":
        for _ in range(rng.randint(5, 10)):
            buy(rng.uniform(5, 25), rng.uniform(0.5, 3.5))
        if launch.dev_tokens > 0:
            events.append(sim.sell(t0 + int(rng.uniform(30, 90) * 1000), launch.dev_tokens, creator))
        for k in range(3):
            sell_others(95 + k * 4, 0.8 if k == 0 else 0.9)
    else:  # grad
        at = 20.0
        for _ in range(200):
            if sim.state.graduated:
                break
            at += rng.uniform(3, 8)
            buy(at, rng.uniform(4, 8))
    return events


def generate(seed: int, n_tokens: int, start_ms: int, first_id: int = 0, mean_gap_ms: float = 4000) -> list[Event]:
    """`n_tokens` launches with their trades and scanner verdicts, sorted by time."""
    rng = random.Random(seed)
    events: list[Event] = []
    t = start_ms
    for i in range(n_tokens):
        t += int(rng.expovariate(1 / mean_gap_ms))
        events += _token(rng, first_id + i, t)
    events.sort(key=lambda e: e.t)
    return events
