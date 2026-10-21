"""Bitcoin 5-minute Up/Down markets: one per 300-second window, slug `btc-updown-5m-<window start, unix seconds>`."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .api import GAMMA, ApiError, get_json

WINDOW = 300


@dataclass(frozen=True)
class Market:
    slug: str
    start: int  # window start, unix seconds
    condition_id: str
    up_token: str
    down_token: str
    winner: int | None  # 0 = Up, 1 = Down, None until resolved

    @property
    def end(self) -> int:
        return self.start + WINDOW


def window_starts(now: int, hours: float, skip_recent: int = 3) -> list[int]:
    """Start times of the 5-minute windows that ended between `hours` ago and a few windows ago (so they are resolved)."""
    last = (now // WINDOW - skip_recent) * WINDOW
    first = last - int(hours * 3600) // WINDOW * WINDOW
    return list(range(first, last + 1, WINDOW))


def load_market(start: int) -> Market | None:
    """The market for the window starting at `start`, or None when it does not exist or has not resolved."""
    slug = f"btc-updown-5m-{start}"
    try:
        rows = get_json(f"{GAMMA}/markets", {"slug": slug, "closed": "true"})
    except ApiError:
        return None
    if not rows:
        return None
    m = rows[0]
    try:
        tokens, outcomes, prices = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"]), json.loads(m["outcomePrices"])
        if outcomes != ["Up", "Down"] or m.get("umaResolutionStatus") != "resolved":
            return None
        win = [float(p) for p in prices]
        winner = 0 if win[0] > 0.99 and win[1] < 0.01 else 1 if win[1] > 0.99 and win[0] < 0.01 else None
    except (KeyError, ValueError, TypeError):
        return None
    if winner is None:
        return None
    return Market(slug, start, m["conditionId"], tokens[0], tokens[1], winner)
