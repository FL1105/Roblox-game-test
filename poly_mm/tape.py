"""The public trade tape of a market: every matched taker trade, with price, size and time. Cached on disk as JSON lines."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .api import DATA, ApiError, get_json
from .markets import Market

CACHE = Path("data/poly/tape")
PAGE = 500


@dataclass(frozen=True)
class Trade:
    t: int  # unix seconds
    outcome: int  # 0 = Up, 1 = Down: the token the taker traded
    side: str  # "BUY" or "SELL": the taker's side
    price: float
    size: float  # shares


def fetch_tape(m: Market, cache: Path = CACHE) -> list[Trade]:
    """All taker trades of the market, oldest first."""
    path = cache / f"{m.slug}.jsonl"
    if path.exists():
        return [Trade(**json.loads(line)) for line in path.read_text().splitlines() if line]
    rows: list[dict] = []
    for page in range(40):
        try:
            batch = get_json(f"{DATA}/trades", {"market": m.condition_id, "limit": PAGE, "offset": page * PAGE, "takerOnly": "true"})
        except ApiError:
            break  # the API stops serving deep offsets; what we have is the most recent part of the tape
        rows += batch
        if len(batch) < PAGE:
            break
    trades = [Trade(int(r["timestamp"]), 0 if r["outcome"] == "Up" else 1, r["side"], float(r["price"]), float(r["size"])) for r in rows if r["outcome"] in ("Up", "Down")]
    trades.reverse()  # the API lists newest first
    trades.sort(key=lambda x: x.t)  # stable: keeps the API's order within the same second
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(t)) + "\n" for t in trades))
    return trades
