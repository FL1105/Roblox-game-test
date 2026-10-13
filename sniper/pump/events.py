"""Event model for the pump.fun stream, plus JSONL (de)serialisation for record/replay.

The PumpPortal field names in `parse_pumpportal` come from its public docs as I remember
them and have not been checked against the live feed. Parsing is deliberately tolerant:
anything that doesn't fit is dropped rather than raising.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Iterable, Union

TOTAL_SUPPLY = 1_000_000_000  # every pump.fun token mints 1B


@dataclass(frozen=True)
class NewToken:
    t: int  # ms since epoch (local receipt time for live data)
    mint: str
    creator: str
    name: str
    symbol: str
    dev_tokens: float  # tokens the creator bought in the create transaction
    dev_sol: float
    v_sol: float  # virtual curve reserves *after* this event
    v_tokens: float


@dataclass(frozen=True)
class Trade:
    t: int
    mint: str
    trader: str
    side: str  # "buy" | "sell"
    sol: float
    tokens: float
    v_sol: float
    v_tokens: float


@dataclass(frozen=True)
class ScanNote:
    """The scanner's verdict on a token, recorded so a replay can reproduce the filter."""

    t: int  # when the verdict became available
    mint: str
    verdict: str
    score: int = 0
    summary: str = ""


Event = Union[NewToken, Trade, ScanNote]

_BY_NAME = {"new_token": NewToken, "trade": Trade, "scan": ScanNote}
_BY_TYPE = {cls: name for name, cls in _BY_NAME.items()}


def encode(ev: Event) -> str:
    d = asdict(ev)
    d["type"] = _BY_TYPE[type(ev)]
    return json.dumps(d, separators=(",", ":"))


def decode(line: str) -> Event:
    d = json.loads(line)
    return _BY_NAME[d.pop("type")](**d)


def read_jsonl(path: str) -> list[Event]:
    events: list[Event] = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                events.append(decode(line))
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError(f"{path}:{n}: bad event line ({e})") from e
    return events


def write_jsonl(path: str, events: Iterable[Event]) -> None:
    with open(path, "w") as f:
        for ev in events:
            f.write(encode(ev) + "\n")


def parse_pumpportal(msg: dict, now_ms: int) -> NewToken | Trade | None:
    """Convert one PumpPortal websocket message, or None if it isn't a pump.fun create/buy/sell."""
    if not isinstance(msg, dict):
        return None
    mint, tx = msg.get("mint"), msg.get("txType")
    if not mint or tx not in ("create", "buy", "sell"):
        return None
    if msg.get("pool", "pump") != "pump":
        return None
    try:
        v_sol = float(msg["vSolInBondingCurve"])
        v_tokens = float(msg["vTokensInBondingCurve"])
        trader = str(msg["traderPublicKey"])
        if tx == "create":
            return NewToken(
                t=now_ms,
                mint=mint,
                creator=trader,
                name=str(msg.get("name") or ""),
                symbol=str(msg.get("symbol") or ""),
                dev_tokens=float(msg.get("initialBuy") or 0),
                dev_sol=float(msg.get("solAmount") or 0),
                v_sol=v_sol,
                v_tokens=v_tokens,
            )
        return Trade(
            t=now_ms,
            mint=mint,
            trader=trader,
            side=tx,
            sol=float(msg.get("solAmount") or 0),
            tokens=float(msg.get("tokenAmount") or 0),
            v_sol=v_sol,
            v_tokens=v_tokens,
        )
    except (KeyError, TypeError, ValueError):
        return None
