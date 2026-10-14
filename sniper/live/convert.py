"""Raw recordings (collector.py) -> replayable events (pump/events.py).

Each change in a token's inferred reserves becomes a Trade whose side and size come from the reserve move. The trader
is unknown ("?"): the quote feed shows how the curve moved, not who moved it, so creator-sell exits cannot fire on
this data. The one-off sell-route check becomes a ScanNote (verdict from the real round trip and the dev's buy).
"""

from __future__ import annotations

import json
from collections import defaultdict

from ..pump.events import TOTAL_SUPPLY, Event, NewToken, ScanNote, Trade
from .infer import FEE

MIN_MOVE = 1e-9  # ignore reserve changes smaller than this (relative): rounding noise, not a trade
UNKNOWN_TRADER = "?"


def verdict_from_roundtrip(dev_tokens: float, rt_in: float | None, rt_back: float | None) -> tuple[str, int, str]:
    """Mirror of the scanner's thresholds, using only what the quote feed can measure."""
    notes = []
    if rt_back is None:
        return "REJECT", 100, "no sell route"
    loss = (1 - rt_back / rt_in) * 100
    score = 0
    if loss >= 30:
        return "REJECT", 100, f"round trip loses {loss:.1f}%"
    if loss >= 10:
        score += 40; notes.append(f"round trip loses {loss:.1f}%")
    elif loss >= 5:
        score += 20; notes.append(f"round trip loses {loss:.1f}%")
    dev_pct = dev_tokens / TOTAL_SUPPLY * 100
    if dev_pct >= 20:
        score += 40; notes.append(f"dev bought {dev_pct:.0f}%")
    elif dev_pct >= 10:
        score += 20; notes.append(f"dev bought {dev_pct:.0f}%")
    verdict = "AVOID" if score >= 40 else "CAUTION" if score >= 15 else "PASS"
    return verdict, score, "; ".join(notes)


def read_raw(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # a half-written last line is expected while a recording is in progress
    return rows


def unpriceable(rows: list[dict]) -> set[str]:
    """Mints whose quotes do not follow the standard curve: mayhem-mode launches, tokens that failed the second-size
    check, and (older recordings) tokens whose first three-quote snapshot disagreed with the launch k."""
    out = set()
    for r in rows:
        if r.get("k") == "create" and r.get("mayhem"):
            out.add(r["mint"])
        elif r.get("k") == "end" and r.get("reason") == "off_curve":
            out.add(r["mint"])
        elif r.get("k") == "obs" and abs(r.get("k_ratio", 1.0) - 1) > 5e-3:
            out.add(r["mint"])
    return out


def to_events(rows: list[dict], fee: float = FEE) -> list[Event]:
    skip = unpriceable(rows)
    rows = [r for r in rows if r.get("mint") not in skip]
    launches: dict[str, NewToken] = {}
    last: dict[str, tuple[float, float]] = {}
    events: list[Event] = []
    for r in rows:
        k, mint = r.get("k"), r.get("mint")
        if k == "create":
            nt = NewToken(r["t"], mint, r.get("creator") or "", r.get("name", ""), r.get("symbol", ""), r["dev_tokens"], r["dev_sol"], r["v_sol"], r["v_tokens"])
            launches[mint], last[mint] = nt, (r["v_sol"], r["v_tokens"])
            events.append(nt)
        elif k == "obs" and mint in launches:
            vs0, vt0 = last[mint]
            vs, vt = r["v_sol"], r["v_tokens"]
            if abs(vt - vt0) / vt0 > MIN_MOVE:
                if vt < vt0:  # tokens left the curve: someone bought
                    side, sol = "buy", (vs - vs0) / (1 - fee)
                else:
                    side, sol = "sell", (vs0 - vs) * (1 - fee)
                events.append(Trade(r["t"], mint, UNKNOWN_TRADER, side, abs(sol), abs(vt0 - vt), vs, vt))
                last[mint] = (vs, vt)
            if "rt_back_sol" in r:  # older recordings kept the sell check on the first observation
                v, score, summary = verdict_from_roundtrip(launches[mint].dev_tokens, r.get("rt_in_sol"), r.get("rt_back_sol"))
                events.append(ScanNote(r["t"], mint, v, score, summary))
        elif k == "chk" and mint in launches:
            v, score, summary = verdict_from_roundtrip(launches[mint].dev_tokens, r.get("rt_in_sol"), r.get("rt_back_sol"))
            events.append(ScanNote(r["t"], mint, v, score, summary))
    return sorted(events, key=lambda e: e.t)  # stable: a trade and its scan keep recording order


def token_paths(rows: list[dict]) -> dict[str, dict]:
    """Per token: create record, observation list, how it ended. Used for analysis and model training.
    Tokens whose curve we cannot price are left out."""
    skip = unpriceable(rows)
    out: dict[str, dict] = defaultdict(lambda: {"create": None, "obs": [], "end": None})
    for r in rows:
        if r.get("mint") in skip:
            continue
        k, mint = r.get("k"), r.get("mint")
        if k == "create":
            out[mint]["create"] = r
        elif k == "obs":
            out[mint]["obs"].append(r)
        elif k == "end":
            out[mint]["end"] = r
    return {m: v for m, v in out.items() if v["create"]}
