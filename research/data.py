"""Recorded launches as `Token` records, in launch order, plus the market-wide view a feature may look at."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from sniper.live.convert import read_raw, to_events, token_paths, unpriceable
from sniper.pump.events import TOTAL_SUPPLY, Event, NewToken, ScanNote, Trade

SEALED_MARKERS = ("holdout", "sealed", "run2")  # recordings reserved for research.confirm


@dataclass
class Token:
    idx: int                      # position in launch order
    mint: str
    creator: str
    name: str
    symbol: str
    t_launch: int                 # ms
    dev_tokens: float
    dev_sol: float
    v_sol0: float
    v_tokens0: float
    mayhem: bool
    obs: list[tuple[int, float, float]]       # (t, v_sol, v_tokens) after launch, time-ordered
    check: tuple[int, float, float] | None    # (t, sol_in, sol_back) of the one-off sell-route round trip
    end_reason: str | None
    end_t: int | None
    events: list[Event] = field(default_factory=list, repr=False)  # engine events (launch, trades, one scan note)

    @property
    def dev_pct(self) -> float:
        return self.dev_tokens / TOTAL_SUPPLY * 100

    @property
    def first_quote_t(self) -> int | None:
        return self.obs[0][0] if self.obs else None


def guard_path(path: str, unseal: bool = False) -> None:
    low = os.path.basename(path).lower()
    if any(m in low for m in SEALED_MARKERS) and not unseal:
        raise PermissionError(f"{path} is a sealed recording: only research.confirm may open it")


def load(paths: list[str], unseal: bool = False) -> list[Token]:
    """Priceable launches from raw recordings, sorted by launch time. Mayhem-mode and off-curve tokens are dropped (unpriceable)."""
    rows: list[dict] = []
    for p in paths:
        guard_path(p, unseal)
        rows += read_raw(p)
    rows.sort(key=lambda r: r["t"])  # stable: keeps recording order for equal timestamps
    skip = unpriceable(rows)
    paths_by_mint = token_paths(rows)
    events: dict[str, list[Event]] = {}
    for ev in to_events(rows):
        events.setdefault(ev.mint, []).append(ev)
    checks: dict[str, tuple[int, float, float]] = {}
    for r in rows:
        if r.get("k") == "chk" and r["mint"] not in skip and "rt_back_sol" in r and r["mint"] not in checks:
            back = r["rt_back_sol"]
            checks[r["mint"]] = (r["t"], r.get("rt_in_sol") or 0.1, 0.0 if back is None else back)  # no sell route at all = nothing came back
    tokens: list[Token] = []
    for mint, p in paths_by_mint.items():
        c, end = p["create"], p["end"] or {}
        obs = sorted(((r["t"], r["v_sol"], r["v_tokens"]) for r in p["obs"]), key=lambda x: x[0])
        tokens.append(Token(0, mint, c.get("creator") or "", c.get("name", ""), c.get("symbol", ""), c["t"], c["dev_tokens"], c["dev_sol"],
                            c["v_sol"], c["v_tokens"], bool(c.get("mayhem")), obs, checks.get(mint), end.get("reason"), end.get("t"), events.get(mint, [])))
    tokens.sort(key=lambda t: (t.t_launch, t.mint))
    for i, t in enumerate(tokens):
        t.idx = i
    return tokens


def fingerprint(tokens: list[Token]) -> str:
    h = hashlib.sha256()
    for t in tokens:
        h.update(f"{t.mint}|{t.t_launch}|{len(t.obs)}|{t.obs[-1][1] if t.obs else 0}".encode())
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class Seen:
    """A token as it looked at some moment: only what had happened by then."""
    mint: str
    creator: str
    name: str
    symbol: str
    t_launch: int
    dev_pct: float
    dev_sol: float
    obs: tuple[tuple[int, float, float], ...]   # observations with t <= the moment
    check: tuple[int, float, float] | None      # the sell-route check, if it had been made by then
    ended: bool                                  # tracking had ended by then

    @property
    def last_price(self) -> float | None:
        return self.obs[-1][1] / self.obs[-1][2] if self.obs else None


class Market:
    """Everything recorded, reachable only through `view(t)`, which returns what had happened by time t."""

    def __init__(self, tokens: list[Token]):
        self._tokens = tokens
        self._launch_times = [t.t_launch for t in tokens]

    def view(self, t: int, exclude_idx: int | None = None) -> list[Seen]:
        """Tokens launched at or before t (oldest first), each cut at t. `exclude_idx` leaves out the token being decided on."""
        out = []
        for tok in self._tokens:
            if tok.t_launch > t:
                break
            if tok.idx == exclude_idx:
                continue
            obs = tuple(o for o in tok.obs if o[0] <= t)
            chk = tok.check if tok.check is not None and tok.check[0] <= t else None
            out.append(Seen(tok.mint, tok.creator, tok.name, tok.symbol, tok.t_launch, tok.dev_pct, tok.dev_sol, obs, chk,
                            tok.end_t is not None and tok.end_t <= t))
        return out
