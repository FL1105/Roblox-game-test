"""What buying a launch at the decision time would have returned, under a named exit policy (the engine's own fees and delay)."""

from __future__ import annotations

import os
from dataclasses import replace

import numpy as np

from sniper.paper.engine import PaperEngine, Strategy
from sniper.pump.events import NewToken, ScanNote, Trade

from .data import Token
from .paths import CACHE_DIR
from .versioning import code_hash

LATENCY_MS = 800   # decision-to-fill delay, as everywhere else in the project
SIZE_SOL = 0.1

# Entry rules that are the same for every policy: always buy once we decide, whatever the token looks like
# (selection is the model's job, not the engine's), never abort for slippage, no creator-sell exit (the data can't show it).
BASE = Strategy(
    latency_ms=LATENCY_MS, size_sol=SIZE_SOL, size_caution_sol=None, size_mode="fixed", auto_compound=False, max_open=10_000,
    require_scan=True, allowed_verdicts=frozenset({"PASS"}), max_dev_buy_pct=100.0, max_entry_mcap_sol=10_000.0, slippage_pct=50.0,
    exit_on_creator_sell=False, use_blacklist=False,
    min_liquidity_sol=-1e9,   # the engine's default of 0 skips any token whose price is at or below its launch price: a hidden filter on the universe
)

POLICIES: dict[str, dict] = {
    "tp30sl15": dict(take_profit_pct=30, stop_loss_pct=15, max_hold_s=300),
    "tp15sl10": dict(take_profit_pct=15, stop_loss_pct=10, max_hold_s=120),
    "tp60sl25": dict(take_profit_pct=60, stop_loss_pct=25, max_hold_s=600),
    "smart": dict(exit_mode="smart", take_profit_pct=30, stop_loss_pct=20, max_hold_s=600),
    "trail12": dict(take_profit_pct=500, stop_loss_pct=20, trailing_stop_pct=12, max_hold_s=300),
    "hold60": dict(take_profit_pct=1000, stop_loss_pct=99, max_hold_s=60),
    "hold300": dict(take_profit_pct=1000, stop_loss_pct=99, max_hold_s=300),
    "ladder": dict(take_profit_pct=30, stop_loss_pct=15, trailing_stop_pct=12, max_hold_s=600, ladder=((30.0, 50.0), (60.0, 25.0))),
}
MAX_EXIT_WINDOW_S = 600  # the longest any policy can hold; used for the embargo between train and test


def strategy(policy: str, cost: float) -> Strategy:
    return replace(BASE, tx_cost_sol=cost, **POLICIES[policy])


def decision_time(tok: Token, delay_s: float) -> int | None:
    """When we would decide: `delay_s` after the first quote we could have seen. None if the token was never quoted."""
    return None if not tok.obs else tok.obs[0][0] + int(delay_s * 1000)


def tradeable(tok: Token, delay_s: float) -> bool:
    """A decision needs a quote at or before decision time, and a token still on its curve then (not graduated, not gone).
    Launches in the tail of their recording (cut by launch time, for every policy alike) are outside the evaluation."""
    t0 = decision_time(tok, delay_s)
    if t0 is None or tok.tail:
        return False
    seen = [o for o in tok.obs if o[0] <= t0]
    if not seen:
        return False
    return seen[-1][2] > 280_000_000  # v_tokens above the graduation line


TICK_MS = 5_000


def net_return(tok: Token, policy: str, delay_s: float, cost: float) -> float:
    """Net return (%) of buying SIZE_SOL at the decision time and exiting by `policy`; NaN only if we could not have decided (see `tradeable`).

    A token nobody touched after the quote still gets a fill (at the last quoted price) and an exit: quiet clock ticks stand in for
    the passing time, so 'no more trades' means a small loss to fees rather than 'no outcome' (dropping those would quietly
    select the tokens that went on to trade). A token whose recording ended before the exit rule would have fired is closed at its
    last known price, as `PaperEngine.finish` does. A tradeable token that still produces no trade is a harness bug and raises."""
    if not tradeable(tok, delay_s):
        return float("nan")
    t0 = decision_time(tok, delay_s)
    stream = [ev for ev in tok.events if not isinstance(ev, ScanNote)]  # the recorded scan's timing is not our decision time
    stream.append(ScanNote(t0, tok.mint, "PASS", 0, ""))
    horizon = t0 + LATENCY_MS + int(POLICIES[policy].get("max_hold_s", MAX_EXIT_WINDOW_S) * 1000) + 2 * TICK_MS
    stream += [ScanNote(t, "__tick__", "PASS", 0, "") for t in range(t0 + TICK_MS, horizon, TICK_MS)]
    stream.sort(key=lambda e: e.t)  # stable: a trade and our note at the same millisecond keep their order
    engine = PaperEngine(strategy(policy, cost), 10.0)
    for ev in stream:
        engine.on_event(ev)
    engine.finish()
    if not engine.closed:
        raise RuntimeError(f"no trade for tradeable token {tok.mint}: engine skipped {dict(engine.skipped)}")
    return engine.closed[0].ret_pct


class OutcomeCache:
    """Net returns for every token under a (policy, delay, cost) setting, computed once and kept on disk (written atomically, keyed by every
    piece of code and data that decides the number)."""

    def __init__(self, tokens: list[Token], fingerprint: str, directory: str = CACHE_DIR):
        self.tokens, self.fp, self.dir = tokens, fingerprint, str(directory)
        os.makedirs(self.dir, exist_ok=True)
        self._mem: dict[tuple, np.ndarray] = {}

    def _path(self, key: tuple) -> str:
        return os.path.join(self.dir, f"out_{self.fp}_{code_hash()}_{'_'.join(str(k) for k in key)}.npy")

    def returns(self, policy: str, delay_s: float, cost: float) -> np.ndarray:
        key = (policy, float(delay_s), float(cost))
        if key not in self._mem:
            path = self._path(key)
            arr = None
            if os.path.exists(path):
                try:
                    arr = np.load(path)
                    if len(arr) != len(self.tokens):
                        arr = None
                except (OSError, ValueError, EOFError):
                    arr = None
            if arr is None:
                arr = np.array([net_return(t, policy, delay_s, cost) for t in self.tokens], dtype=float)
                atomic_save_npy(path, arr)
            self._mem[key] = arr
        return self._mem[key]


def atomic_save_npy(path: str, arr: np.ndarray) -> None:
    tmp = f"{path}.{os.getpid()}.tmp.npy"
    np.save(tmp, arr)
    os.replace(tmp, path)
