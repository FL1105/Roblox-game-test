"""Live dashboard for the paper bot: a runner thread drives the bot against the live books and a loopback-only server shows its P&L.

It places no orders and has no key fields. The server binds to 127.0.0.1, refuses foreign Host headers and takes only JSON POSTs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from kalshi_btc15 import api

from . import bot as botmod
from . import btc3

STATIC = Path(__file__).with_name("static") / "dashboard.html"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
SETTLE_AFTER_S = 150


class Runtime:
    """Everything the dashboard shows that is not the bot itself. All access goes through `lock`."""

    def __init__(self, bot: botmod.PaperBot, interval: float = 6.0, actuals_path: str | None = None):
        self.bot, self.interval, self.actuals_path = bot, interval, actuals_path
        self.lock = threading.RLock()
        self.last_row: dict | None = None
        self.last_poll_t: float | None = None
        self.polls = self.errors = 0
        self.started = time.time()
        self.log: deque = deque(maxlen=200)
        self.actuals: dict[str, float] = {}
        self.stop = threading.Event()
        self._settle_try: dict[int, float] = {}
        if actuals_path and os.path.exists(actuals_path):
            try:
                with open(actuals_path) as f:
                    self.actuals = {str(k): float(v) for k, v in json.load(f).items()}
            except (OSError, ValueError):
                pass

    def say(self, text: str) -> None:
        self.log.append([time.time(), text])

    def set_actual(self, close: int, pnl: float | None) -> None:
        with self.lock:
            if pnl is None:
                self.actuals.pop(str(close), None)
            else:
                self.actuals[str(close)] = float(pnl)
            if self.actuals_path:
                tmp = self.actuals_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.actuals, f)
                os.replace(tmp, self.actuals_path)

    def tick(self, poll=btc3.poll_sync, outcomes=botmod.outcomes, now: float | None = None) -> None:
        """One iteration: poll the books, let the bot act, settle windows that have resolved."""
        try:
            rows = poll()
            err = None
        except Exception as e:                      # the dashboard must survive any single bad poll
            rows, err = [], f"{type(e).__name__}: {e}"
        t = time.time() if now is None else now
        with self.lock:
            self.polls += 1
            if err:
                self.errors += 1
                self.say(f"poll failed: {err[:120]}")
            for r in rows:
                self.last_row, self.last_poll_t = r, t
                trade = self.bot.on_row(r, now=t)
                if trade:
                    self.say(f"PAPER TRADE {trade['sets']} sets: YES {trade['yes_on']} @{trade['price_yes']:.3f} + NO {trade['no_on']} @{trade['price_no']:.3f} "
                             f"(${trade['cost']:.0f} deployed, +${trade['net_if_agree']:.2f} if the venues agree)")
            due = [c for c in list(self.bot.trades) if t > c + SETTLE_AFTER_S and t - self._settle_try.get(c, 0) > 20]
        for close in due:                           # network calls outside the lock
            self._settle_try[close] = t
            try:
                up = outcomes(close)
            except Exception:
                up = None
            if up is not None:
                with self.lock:
                    pnl = self.bot.settle(close, up)
                    self.say(f"SETTLED window {time.strftime('%H:%M', time.gmtime(close))}Z: ${pnl:+.2f}")

    def run(self) -> None:
        while not self.stop.is_set():
            t0 = time.time()
            self.tick()
            self.stop.wait(max(0.0, self.interval - (time.time() - t0)))


def build_state(rt: Runtime, now: float | None = None) -> dict:
    """The JSON the page renders."""
    now = time.time() if now is None else now
    with rt.lock:
        bot, L = rt.bot, rt.bot.limits
        wins: dict[int, dict] = {}
        for t in bot.all_trades:
            w = wins.setdefault(t["close"], {"close": t["close"], "trades": 0, "deployed": 0.0, "if_agree": 0.0, "paper": 0.0, "settled_n": 0, "payoffs": [0, 0, 0]})
            w["trades"] += 1
            w["deployed"] += t["cost"]
            w["if_agree"] += t["net_if_agree"]
        for s in bot.settled:
            w = wins.get(s["close"])
            if w is not None:
                w["paper"] += s["realised"]
                w["settled_n"] += 1
                w["payoffs"][s["payoff"]] += 1
        rows, cum, equity = [], 0.0, []
        for close in sorted(wins):
            w = wins[close]
            w["settled"] = w["settled_n"] >= w["trades"]
            w["actual"] = rt.actuals.get(str(close))
            if w["settled"]:
                cum += w["paper"]
                equity.append([close, round(cum, 2)])
            rows.append(w)
        settled_w = [w for w in rows if w["settled"]]
        payoffs = [sum(w["payoffs"][k] for w in settled_w) for k in range(3)]
        open_trades = [t for ts in bot.trades.values() for t in ts]
        row = rt.last_row
        market = None
        if row is not None:
            now_best = bot.best(row)
            market = {"close": row["close"], "left_s": row["left_s"], "skew_s": row.get("skew_s"), "quotes": row["quotes"],
                      "best": None if now_best is None else {k: now_best[k] for k in ("yes_on", "no_on", "sets", "net")}}
        window_used = sum(t["cost"] for t in bot.trades.get(row["close"], [])) if row else 0.0
        age = None if rt.last_poll_t is None else now - rt.last_poll_t
        with_actual = [w for w in settled_w if w["actual"] is not None]
        return {
            "now": now, "mode": "PAPER", "paused": bot.paused, "reason": bot.reason,
            "health": {"polls": rt.polls, "errors": rt.errors, "age_s": age, "stale": age is None or age > 4 * rt.interval + 5,
                       "uptime_s": now - rt.started, "interval": rt.interval},
            "limits": {"max_sets": L.max_sets, "max_window_cost": L.max_window_cost, "max_open_cost": L.max_open_cost,
                       "daily_loss_stop": L.daily_loss_stop, "min_net_per_set": L.min_net_per_set, "fill_fraction": L.fill_fraction},
            "usage": {"window_cost": window_used, "open_cost": bot.open_cost(), "day_pnl": bot.day_realised(now)},
            "totals": {"today": bot.day_realised(now), "all_time": bot.realised, "open_if_agree": sum(t["net_if_agree"] for t in open_trades),
                       "open_cost": sum(t["cost"] for t in open_trades), "trades": len(bot.all_trades), "windows": len(rows),
                       "windows_settled": len(settled_w), "windows_positive": sum(1 for w in settled_w if w["paper"] > 0),
                       "payoffs": payoffs, "actual_sum": sum(w["actual"] for w in with_actual), "paper_sum_actual_windows": sum(w["paper"] for w in with_actual),
                       "actual_windows": len(with_actual)},
            "equity": equity, "windows": rows[-40:], "open": open_trades, "market": market,
            "log": list(rt.log)[-40:],
        }


def host_of(raw: str) -> str:
    """The host part of a Host header: 'localhost:8766' -> 'localhost', '[::1]:8766' -> '[::1]'."""
    if raw.startswith("["):
        return raw.split("]")[0] + "]"
    return raw.rsplit(":", 1)[0] if ":" in raw else raw


def make_server(rt: Runtime, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            return host_of(self.headers.get("Host") or "") in LOCAL_HOSTS

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "foreign host"})
            if self.path in ("/", "/index.html"):
                return self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._json(200, build_state(rt))
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "foreign host"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "content-type must be application/json"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10_000)) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if self.path == "/api/pause" and isinstance(body.get("paused"), bool):
                with rt.lock:
                    rt.bot.paused = body["paused"]
                    rt.say("PAUSED by you: no new paper trades" if body["paused"] else "RESUMED by you")
                return self._json(200, {"paused": body["paused"]})
            if self.path == "/api/actual" and isinstance(body.get("close"), int) and (body.get("pnl") is None or isinstance(body.get("pnl"), (int, float))):
                rt.set_actual(body["close"], body.get("pnl"))
                return self._json(200, {"ok": True})
            self._json(400, {"error": "unknown action"})

    return ThreadingHTTPServer((host, port), Handler)
