"""Settings. Everything the dashboard can change is validated here, in one place."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace

from .assets import Asset

KNOWN: dict[str, Asset] = {
    "XRP": Asset("XRP"),
    "RLUSD": Asset("524C555344000000000000000000000000000000", "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"),
    "USD.Bitstamp": Asset("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"),
    "USD.GateHub": Asset("USD", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"),
    "EUR.GateHub": Asset("EUR", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"),
    "SOLO": Asset("534F4C4F00000000000000000000000000000000", "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz"),
    "CORE": Asset("434F524500000000000000000000000000000000", "rcoreNywaoz2ZCQ8Lg2EbSLnGuRBmun6D"),
}

DEFAULT_RPC = ("https://xrplcluster.com", "https://s1.ripple.com:51234", "https://s2.ripple.com:51234")
DEFAULT_MARKETS = (
    ("XRP", "RLUSD"), ("XRP", "USD.Bitstamp"), ("XRP", "USD.GateHub"), ("XRP", "EUR.GateHub"), ("XRP", "SOLO"), ("XRP", "CORE"),
    ("SOLO", "CORE"), ("RLUSD", "USD.Bitstamp"),
)

# name -> (type, min, max); the dashboard may change these while the bot runs
TUNABLE: dict[str, tuple[type, float, float]] = {
    "min_profit_bps": (float, 0.0, 1000.0),
    "min_profit_base": (float, 0.0, 1000.0),
    "size_min": (float, 0.01, 1e6),
    "size_max": (float, 0.01, 1e6),
    "slip_bps": (float, 0.0, 500.0),
    "max_loss_bps": (float, 0.0, 2000.0),
    "cooldown_ledgers": (int, 0, 1000),
}


@dataclass(frozen=True)
class Config:
    base: str = "XRP"
    markets: tuple[tuple[str, str], ...] = DEFAULT_MARKETS
    max_hops: int = 3
    size_min: float = 10.0  # base units
    size_max: float = 500.0
    min_profit_bps: float = 10.0  # net of fees, a trade must clear both of these
    min_profit_base: float = 0.05
    slip_bps: float = 10.0  # how far a non-final leg may fill below its quote before it is abandoned
    max_loss_bps: float = 100.0  # an unwind may sell this far below the entry before it is abandoned
    cooldown_ledgers: int = 3  # after trading a route, ledgers before trading it again
    book_depth: int = 40
    fee_drops: int = 12  # per transaction, used for the cost estimate
    poll_s: float = 1.0

    def assets(self) -> dict[str, Asset]:
        names = {self.base} | {n for m in self.markets for n in m}
        missing = sorted(n for n in names if n not in KNOWN)
        if missing:
            raise ValueError(f"unknown asset(s) {missing}; known: {sorted(KNOWN)}")
        return {n: KNOWN[n] for n in names}

    def validate(self) -> "Config":
        self.assets()
        if not any(self.base in m for m in self.markets):
            raise ValueError(f"base asset {self.base} is not in any watched market")
        if not 2 <= self.max_hops <= 4:
            raise ValueError("max_hops must be 2 to 4")
        if self.size_min > self.size_max:
            raise ValueError("size_min must not exceed size_max")
        if not self.markets:
            raise ValueError("need at least one market")
        return self

    def update(self, data: dict) -> "Config":
        """Apply a partial update of tunable settings; raises ValueError naming the bad field."""
        changes = {}
        for k, v in data.items():
            if k not in TUNABLE:
                raise ValueError(f"unknown setting {k!r}")
            typ, lo, hi = TUNABLE[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (typ is int and v != int(v)) or not math.isfinite(v):
                raise ValueError(f"{k} must be a number")
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
            changes[k] = typ(v)
        return replace(self, **changes).validate()

    def tunables(self) -> dict:
        return {k: getattr(self, k) for k in TUNABLE}


__all__ = ["Config", "KNOWN", "DEFAULT_RPC", "TUNABLE", "fields"]
