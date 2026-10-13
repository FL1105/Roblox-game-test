"""Local launch history. Deployer reputation is built from launches we have observed ourselves,
so it needs no paid indexer, but it starts empty and only knows creators seen since you began recording."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from .curve import GRADUATION_V_TOKENS
from .events import NewToken, Trade


@dataclass(frozen=True)
class Outcome:
    rugged: bool
    creator_sold_pct: float  # of what the creator bought, sold inside the observation window
    peak_mcap_sol: float
    final_mcap_sol: float
    graduated: bool


@dataclass(frozen=True)
class DeployerProfile:
    launches_24h: int = 0
    launches_total: int = 0
    rugs_total: int = 0
    graduated_total: int = 0
    # From the wallet's recent transactions (None when not looked up)
    wallet_age_hours: float | None = None
    wallet_age_is_lower_bound: bool = False
    wallet_tx_visible: int | None = None


class OutcomeTracker:
    """Follows one token's trades during its observation window and classifies how it ended."""

    # "Rug" here means: the creator dumped at least half of what they bought AND the price
    # ended at least 50% below its peak. Plain abandonment is not counted.
    RUG_SOLD_FRACTION = 0.5
    RUG_DRAWDOWN = 0.5

    def __init__(self, launch: NewToken):
        self.creator = launch.creator
        self.creator_bought = launch.dev_tokens
        self.creator_sold = 0.0
        self.peak_mcap = self.last_mcap = launch.v_sol / launch.v_tokens * 1e9
        self.graduated = False

    def on_trade(self, tr: Trade) -> None:
        mcap = tr.v_sol / tr.v_tokens * 1e9
        self.last_mcap = mcap
        self.peak_mcap = max(self.peak_mcap, mcap)
        if tr.trader == self.creator:
            if tr.side == "buy":
                self.creator_bought += tr.tokens
            else:
                self.creator_sold += tr.tokens
        if tr.v_tokens <= GRADUATION_V_TOKENS:
            self.graduated = True

    def finalize(self) -> Outcome:
        sold_pct = self.creator_sold / self.creator_bought if self.creator_bought > 0 else 0.0
        drawdown = 1 - self.last_mcap / self.peak_mcap if self.peak_mcap > 0 else 0.0
        rugged = (
            sold_pct >= self.RUG_SOLD_FRACTION and drawdown >= self.RUG_DRAWDOWN and not self.graduated
        )
        return Outcome(rugged, sold_pct, self.peak_mcap, self.last_mcap, self.graduated)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS launches (
    mint TEXT PRIMARY KEY, creator TEXT NOT NULL, created_ms INTEGER NOT NULL,
    name TEXT, symbol TEXT, dev_tokens REAL
);
CREATE INDEX IF NOT EXISTS launches_creator ON launches(creator, created_ms);
CREATE TABLE IF NOT EXISTS outcomes (
    mint TEXT PRIMARY KEY, rugged INTEGER NOT NULL, creator_sold_pct REAL,
    peak_mcap_sol REAL, final_mcap_sol REAL, graduated INTEGER NOT NULL
);
"""

DAY_MS = 86_400_000


class LaunchStore:
    def __init__(self, path: str = ":memory:"):
        # Scans run in executor threads while the event loop writes, so serialise access.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._db.executescript(_SCHEMA)

    def add_launch(self, nt: NewToken) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO launches VALUES (?,?,?,?,?,?)",
                (nt.mint, nt.creator, nt.t, nt.name, nt.symbol, nt.dev_tokens),
            )
            self._db.commit()

    def add_outcome(self, mint: str, o: Outcome) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?)",
                (mint, int(o.rugged), o.creator_sold_pct, o.peak_mcap_sol, o.final_mcap_sol, int(o.graduated)),
            )
            self._db.commit()

    def profile(self, creator: str, now_ms: int, exclude_mint: str | None = None) -> DeployerProfile:
        """History of `creator`'s earlier launches (the launch being scanned is excluded)."""
        with self._lock:
            row = self._db.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN l.created_ms >= ? THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(o.rugged), 0),
                       COALESCE(SUM(o.graduated), 0)
                FROM launches l LEFT JOIN outcomes o ON o.mint = l.mint
                WHERE l.creator = ? AND l.mint != ?
                """,
                (now_ms - DAY_MS, creator, exclude_mint or ""),
            ).fetchone()
        return DeployerProfile(launches_total=row[0], launches_24h=row[1], rugs_total=row[2], graduated_total=row[3])

    def close(self) -> None:
        self._db.close()
