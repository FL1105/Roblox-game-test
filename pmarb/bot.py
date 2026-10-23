"""Paper-trading bot for the Kalshi / Polymarket BTC 15-minute hedge. It places NO orders: it decides, simulates a delayed half-size
fill against the live books, holds each hypothetical position to the real settlement and keeps a ledger.

Live execution is deliberately not implemented: it needs the user's own keys and cannot be tested without them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from kalshi_btc15 import api

from . import btc3, venues
from .replay import FEES

VENUES = ("kalshi", "poly")


@dataclass(frozen=True)
class Limits:
    max_sets: int = 100               # per trade
    max_window_cost: float = 300.0    # dollars deployed in one 15-minute window
    max_open_cost: float = 1000.0     # dollars deployed across windows not yet settled
    daily_loss_stop: float = -300.0   # stop opening trades once realised P&L is below this
    min_net_per_set: float = 0.02     # after fees, at the (delayed) execution quotes
    fill_fraction: float = 0.5        # of the thinner displayed side
    min_left_s: int = 45
    max_left_s: int = 840
    max_skew_s: float = 1.5           # the venues' answers must arrive this close together


@dataclass
class PaperBot:
    limits: Limits = field(default_factory=Limits)
    ledger_path: str | None = None
    pending: dict | None = None
    run_active: bool = False
    trades: dict[int, list[dict]] = field(default_factory=dict)      # window close -> open paper trades
    realised: float = 0.0
    settled: list[dict] = field(default_factory=list)
    all_trades: list[dict] = field(default_factory=list)             # every paper trade ever opened (open or settled)
    paused: bool = False
    reason: str = "starting"                                          # why the bot is or is not trading right now

    # -- decisions -----------------------------------------------------------------------------------------------
    def usable(self, row: dict) -> bool:
        q = row["quotes"]
        if not all(v in q for v in VENUES) or not self.limits.min_left_s <= row["left_s"] <= self.limits.max_left_s:
            return False
        if row.get("skew_s") is None or row["skew_s"] > self.limits.max_skew_s:
            return False
        return all(q[v]["bid"] is not None and q[v]["ask"] is not None and 0.05 <= (q[v]["bid"] + q[v]["ask"]) / 2 <= 0.95 for v in VENUES)

    def best(self, row: dict, fill: float = 1.0, orientation: tuple[str, str] | None = None) -> dict | None:
        q, out = row["quotes"], None
        for a, b in ((orientation,) if orientation else (("kalshi", "poly"), ("poly", "kalshi"))):
            h = btc3.hedge_total(a, q[a], b, q[b], FEES[a], FEES[b], fill)
            if h and (out is None or h["net"] > out["net"]):
                out = h
        return out

    def open_cost(self) -> float:
        return sum(t["cost"] for ts in self.trades.values() for t in ts)

    def day_realised(self, now: float | None = None) -> float:
        """Realised P&L of windows that closed on the current UTC day."""
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))
        return sum(s["realised"] for s in self.settled if time.strftime("%Y-%m-%d", time.gmtime(s["close"])) == day)

    def why_unusable(self, row: dict) -> str:
        q, L = row["quotes"], self.limits
        if not all(v in q for v in VENUES):
            return "waiting for both venues' quotes"
        if row["left_s"] < L.min_left_s:
            return f"last {L.min_left_s}s of the window: no new trades"
        if row["left_s"] > L.max_left_s:
            return "window just opened: waiting for prices to form"
        if row.get("skew_s") is None or row["skew_s"] > L.max_skew_s:
            return "venue answers arrived too far apart"
        return "a side is empty or the price is outside 5-95c"

    def on_row(self, row: dict, now: float | None = None) -> dict | None:
        """Feed one poll. Returns the paper trade executed on it, if any."""
        if self.paused:
            self.pending, self.run_active, self.reason = None, False, "PAUSED: not opening trades"
            return None
        if not self.usable(row):
            self.pending, self.run_active = None, False
            self.reason = self.why_unusable(row)
            return None
        sig = self.best(row)
        profitable = sig is not None and sig["net"] > 0
        trade = None
        if self.pending is not None:                       # a signal from the previous poll: execute now, if still worth it
            p, self.pending = self.pending, None
            if p["close"] == row["close"]:
                trade = self._execute(row, (p["yes_on"], p["no_on"]))
                self.reason = "paper trade placed" if trade else "signal faded or a limit blocked the order"
        elif profitable and not self.run_active:
            if self.day_realised(now) > self.limits.daily_loss_stop:
                self.pending = {"close": row["close"], "yes_on": sig["yes_on"], "no_on": sig["no_on"], "t": row["t"]}
                self.reason = "signal seen: executing on the next poll"
            else:
                self.reason = "daily loss stop reached: not opening trades"
        elif profitable:
            self.reason = "gap still open (already traded this run)"
        else:
            self.reason = "watching: no profitable hedge right now"
        self.run_active = profitable
        return trade

    def _execute(self, row: dict, orientation: tuple[str, str]) -> dict | None:
        L = self.limits
        h = self.best(row, L.fill_fraction, orientation)
        if h is None:
            return None
        a, b = orientation
        pa, pb = row["quotes"][a]["ask"], 1.0 - row["quotes"][b]["bid"]
        window_used = sum(t["cost"] for t in self.trades.get(row["close"], []))
        room = min(L.max_window_cost - window_used, L.max_open_cost - self.open_cost())
        sets = min(h["sets"], L.max_sets, int(room / (pa + pb)) if pa + pb > 0 else 0)
        if sets < 5:                                       # Polymarket's minimum order
            return None
        # re-price at the capped size (fees are not linear because of Kalshi's rounding)
        scaled = btc3.hedge_total(a, {**row["quotes"][a], "ask_size": sets}, b, {**row["quotes"][b], "bid_size": sets}, FEES[a], FEES[b])
        if scaled is None or scaled["net"] / scaled["sets"] < L.min_net_per_set:
            return None
        t = {"event": "trade", "t": row["t"], "close": row["close"], "left_s": row["left_s"], "yes_on": a, "no_on": b, "sets": scaled["sets"],
             "price_yes": pa, "price_no": pb, "cost": scaled["sets"] * (pa + pb), "net_if_agree": scaled["net"]}
        self.trades.setdefault(row["close"], []).append(t)
        self.all_trades.append(t)
        self._log(t)
        return t

    # -- settlement ------------------------------------------------------------------------------------------------
    def settle(self, close: int, up: dict[str, bool]) -> float:
        """Settle every open paper trade of a window against the recorded outcomes ({'kalshi': True if Up, 'poly': ...})."""
        total = 0.0
        for t in self.trades.pop(close, []):
            payoff = int(up[t["yes_on"]]) + int(not up[t["no_on"]])
            real = t["net_if_agree"] + t["sets"] * (payoff - 1)
            total += real
            rec = {"event": "settled", "close": close, "payoff": payoff, "realised": real, "sets": t["sets"], "cost": t["cost"],
                   "yes_on": t["yes_on"], "no_on": t["no_on"]}
            self.settled.append(rec)
            self._log(rec)
        self.realised += total
        return total

    def restore(self, path: str) -> int:
        """Rebuild state from a ledger written by an earlier run: settled trades and any trades still open. Returns lines read."""
        trades: dict[int, list[dict]] = {}
        settled: dict[int, list[dict]] = {}
        n = 0
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    n += 1
                    (trades if rec.get("event") == "trade" else settled if rec.get("event") == "settled" else {}).setdefault(rec.get("close"), []).append(rec)
        except OSError:
            return 0
        for close, ts in sorted(trades.items()):
            self.all_trades += ts
            if len(settled.get(close, [])) < len(ts):                  # not fully settled: still open
                self.trades[close] = list(ts)
        for close, rs in settled.items():
            self.settled += rs
            self.realised += sum(r["realised"] for r in rs)
        return n

    def _log(self, rec: dict) -> None:
        if self.ledger_path:
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def summary(self) -> str:
        n = len(self.settled)
        pay = {k: sum(1 for s in self.settled if s["payoff"] == k) for k in (0, 1, 2)}
        return (f"settled paper trades: {n} (payoffs 0/1/2 = {pay[0]}/{pay[1]}/{pay[2]}), realised ${self.realised:+.2f}; "
                f"open: {sum(len(v) for v in self.trades.values())} trades, ${self.open_cost():.0f} deployed")


def outcomes(close: int) -> dict[str, bool] | None:
    """Recorded outcomes of a window on Kalshi and Polymarket, or None if either has not resolved yet."""
    start = close - 900
    gate = venues._poly_gate
    try:
        k = api.get_json(f"{api.BASE}/markets", {"series_ticker": "KXBTC15M", "limit": 5, "min_close_ts": close - 1, "max_close_ts": close + 1})["markets"]
        ev = api.get_json(f"{venues.GAMMA}/events", {"slug": f"btc-updown-15m-{start}"}, gate=gate)[0]["markets"][0]
        prices, outs = [float(x) for x in json.loads(ev["outcomePrices"])], json.loads(ev["outcomes"])
    except (api.ApiError, IndexError, KeyError, ValueError):
        return None
    if not k or k[0]["result"] not in ("yes", "no") or max(prices) < 0.99:
        return None
    return {"kalshi": k[0]["result"] == "yes", "poly": outs[prices.index(max(prices))] == "Up"}


def run(minutes: float, interval: float, bot: PaperBot, say=print, settle_after_s: int = 150) -> None:
    end = time.time() + minutes * 60
    n = 0
    while time.time() < end or bot.trades:
        t0 = time.time()
        if time.time() < end:
            try:
                rows = btc3.poll_sync()
            except api.ApiError as e:
                say(f"poll failed: {e}")
                rows = []
            n += 1
            for r in rows:
                t = bot.on_row(r)
                if t:
                    say(f"TRADE {time.strftime('%H:%M:%S', time.gmtime(t['t']))}Z {t['sets']} sets YES {t['yes_on']} @{t['price_yes']:.3f} + NO {t['no_on']} @{t['price_no']:.3f}, "
                        f"${t['cost']:.0f} deployed, +${t['net_if_agree']:.2f} if the venues agree, {t['left_s']}s left")
        for close in [c for c in list(bot.trades) if time.time() > c + settle_after_s]:
            up = outcomes(close)
            if up is not None:
                p = bot.settle(close, up)
                say(f"SETTLED window {time.strftime('%H:%M', time.gmtime(close))}Z: realised ${p:+.2f}   | {bot.summary()}")
        if time.time() >= end and time.time() > end + 1500:        # give up waiting for settlements after 25 minutes
            break
        time.sleep(max(0.0, interval - (time.time() - t0)))
    say("FINAL: " + bot.summary())
