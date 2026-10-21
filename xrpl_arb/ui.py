"""Local dashboard for the streaming watcher.

    python -m xrpl_arb ui --config pairs.json            # then open http://127.0.0.1:8765

Serves one self-contained page (static/dashboard.html, no external requests) and its data (/api/state) on 127.0.0.1
only. Read-only like the rest of the package: no key, no transaction, nothing to click that spends anything.
Token names come from the chain and are attacker-controlled, so the page renders them with textContent only; the
server refuses foreign Host headers and sends a Content-Security-Policy that blocks everything but its own page.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .model import XRP, Asset
from .stream import BookState

PAGE = Path(__file__).parent / "static" / "dashboard.html"
LEDGERS_KEPT = 600
ENDED_KEPT = 300
LOG_KEPT = 200


def labels_of(path: list[str]) -> list[str]:
    return [Asset.parse(p).label for p in path]


def opp_view(o, first_ledger: int, last_ledger: int) -> dict:
    """A live opportunity as the page wants it (floats for display; the log keeps the exact strings)."""
    return {
        "key": "|".join(o.path), "labels": labels_of(o.path), "first_ledger": first_ledger, "last_ledger": last_ledger,
        "amount_in": float(o.amount_in), "amount_out": float(o.amount_out), "profit": float(o.profit),
        "profit_pct": float(o.profit_pct),
        "legs": [{"in": l.asset_in.label, "out": l.asset_out.label, "amount_in": float(l.amount_in),
                  "amount_out": float(l.amount_out)} for l in o.legs],
    }


class DashboardState:
    """Everything the page shows, updated from the watcher's events and read by the HTTP handler."""

    def __init__(self, settings: dict, replay: bool = False):
        self.lock = threading.Lock()
        self.settings = settings
        self.replay = replay
        self.started = time.time()
        self.status = {"state": "starting", "detail": ""}
        self.ledgers: deque = deque(maxlen=LEDGERS_KEPT)
        self.live: dict[str, dict] = {}
        self.ended: deque = deque(maxlen=ENDED_KEPT)
        self.log: deque = deque(maxlen=LOG_KEPT)
        self.seen = 0
        self.best_seen: dict | None = None
        self.book: BookState | None = None
        self.markets: list[dict] = []  # fixed rows, used when there is no live BookState (replay)

    # ---- updates -------------------------------------------------------------------------------------------
    def observe(self, ev: dict) -> None:
        with self.lock:
            if ev["kind"] == "status":
                self.status = {"state": ev["state"], "detail": ev["detail"]}
                self._log(None, f"stream {ev['state']}: {ev['detail']}")
            elif ev["kind"] == "ledger":
                self._ledger(ev)

    def _log(self, ledger, text: str) -> None:
        self.log.appendleft({"t": round(time.time(), 1), "ledger": ledger, "text": text})

    def _ledger(self, ev: dict) -> None:
        self.book = ev["state"]
        ledger, cands, opps = ev["ledger"], ev["candidates"], ev["opportunities"]
        top = opps[0] if opps else (cands[0] if cands else None)
        self.ledgers.append({
            "ledger": ledger, "t": ev["t"], "full": ev["full"], "pairs_read": ev["pairs_read"],
            "after_close_s": round(ev["after_close_s"], 3), "read_s": round(ev["read_s"], 3),
            "candidates": len(cands), "opportunities": len(opps), "skipped": len(ev["reads"].skipped),
            "best_gross_xrp": float(cands[0].profit + cands[0].tx_cost) if cands else None,
            "best_labels": labels_of(top.path) if top else None,
        })
        seen_now = set()
        for o in opps:
            view = opp_view(o, ledger, ledger)
            seen_now.add(view["key"])
            prev = self.live.get(view["key"])
            if prev:
                view["first_ledger"] = prev["first_ledger"]
            self.live[view["key"]] = view
            if self.best_seen is None or view["profit"] > self.best_seen["profit"]:
                self.best_seen = {k: view[k] for k in ("labels", "profit", "profit_pct", "amount_in")} | {"ledger": ledger}
        for o in ev["new"]:
            self.seen += 1
            self._log(ledger, f"new {float(o.profit):+.5f} XRP ({float(o.profit_pct):+.2f}%) " + " → ".join(labels_of(o.path)))
        for e in ev["ended"]:
            self.live.pop("|".join(e["path"]), None)
            row = {"labels": labels_of(e["path"]), "first_ledger": e["first_ledger"], "last_ledger": e["last_ledger"],
                   "ledgers_alive": e["ledgers_alive"], "best_profit": float(e["best_profit"]),
                   "best_amount_in": float(e["best_amount_in"])}
            self.ended.appendleft(row)
            self._log(ledger, f"ended after {e['ledgers_alive']} ledger(s), best {row['best_profit']:+.5f} XRP: " + " → ".join(row["labels"]))

    # ---- reading -------------------------------------------------------------------------------------------
    def _market_rows(self) -> list[dict]:
        if self.book is None:
            return self.markets
        in_opp = {frozenset((l["in"], l["out"])) for o in self.live.values() for l in o["legs"]}
        rows = []
        for a, b in self.book.index.pairs:
            key = frozenset((a, b))
            pool = self.book.pools.get(key)
            ab, ba = self.book.books.get((a, b), ()), self.book.books.get((b, a), ())
            row = {"a": a.label, "b": b.label, "offers_ab": len(ab), "offers_ba": len(ba), "pool": None,
                   "changed": self.book.changed_at.get(key), "in_opportunity": frozenset((a.label, b.label)) in in_opp}
            if pool is not None:
                ra, rb = (pool.ra, pool.rb) if pool.a == a else (pool.rb, pool.ra)
                row["pool"] = {"reserve_a": float(ra), "reserve_b": float(rb), "fee_pct": float(pool.fee * 100),
                               "price": float(ra / rb) if rb else None,  # units of a per unit of b
                               "tvl_xrp": float(2 * (ra if a == XRP else rb)) if XRP in (a, b) else None}
            rows.append(row)
        return rows

    def snapshot(self) -> dict:
        with self.lock:
            ledgers = list(self.ledgers)
            recent = [r["after_close_s"] for r in ledgers[-100:] if not r["full"]]
            lifetimes = [e["ledgers_alive"] for e in self.ended]
            return {
                "replay": self.replay, "now": round(time.time(), 1),
                "status": {**self.status, "uptime_s": round(time.time() - self.started),
                           "ledger": ledgers[-1]["ledger"] if ledgers else None},
                "settings": self.settings,
                "kpis": {
                    "live": len(self.live), "seen": self.seen, "ended": len(self.ended),
                    "median_lifetime": statistics.median(lifetimes) if lifetimes else None,
                    "best_seen": self.best_seen, "ledgers": len(ledgers),
                    "median_after_close_s": statistics.median(recent) if recent else None,
                    "p90_after_close_s": sorted(recent)[int(len(recent) * 0.9)] if recent else None,
                },
                "ledgers": ledgers,
                "live": sorted(self.live.values(), key=lambda o: -o["profit"]),
                "ended": list(self.ended),
                "markets": self._market_rows(),
                "log": list(self.log),
            }

    # ---- replay of a recorded --out file -------------------------------------------------------------------
    @classmethod
    def from_rows(cls, rows: list[dict], settings: dict, markets: list[dict] | None = None) -> "DashboardState":
        """Rebuild the page's state from the JSONL a watch run recorded (ledger, opp and end rows)."""
        st = cls(settings, replay=True)
        st.status = {"state": "replay", "detail": "recorded run"}
        st.markets = markets or []
        by_ledger: dict[int, list[dict]] = {}
        for r in rows:
            if r["type"] == "opp":
                by_ledger.setdefault(r["ledger"], []).append(r)
        live: dict[str, dict] = {}
        for r in rows:
            if r["type"] == "ledger":
                first_opps = by_ledger.get(r["ledger"], [])
                best = r.get("best_candidate") or (first_opps[0] if first_opps else None)
                gross = r.get("best_gross_xrp")
                if gross is None and best:  # rows recorded before best_gross_xrp existed: profit is net of the fee
                    gross = float(best["profit"]) + float(best["tx_cost"])
                st.ledgers.append({
                    "ledger": r["ledger"], "t": r["t"], "full": r["full"], "pairs_read": r["pairs_read"],
                    "after_close_s": r["after_close_s"], "read_s": r["read_s"], "candidates": r["candidates"],
                    "opportunities": r["opportunities"], "skipped": r["skipped"],
                    "best_gross_xrp": gross, "best_labels": labels_of(best["path"]) if best else None})
            elif r["type"] == "opp":
                st.seen += 1
                profit = float(r["profit"])
                view = {"key": "|".join(r["path"]), "labels": labels_of(r["path"]), "first_ledger": r["ledger"],
                        "last_ledger": r["ledger"], "amount_in": float(r["amount_in"]), "amount_out": float(r["amount_out"]),
                        "profit": profit, "profit_pct": r["profit_pct"],
                        "legs": [{"in": Asset.parse(l["in"]).label, "out": Asset.parse(l["out"]).label,
                                  "amount_in": float(l["amount_in"]), "amount_out": float(l["amount_out"])} for l in r["legs"]]}
                live[view["key"]] = view
                st._log(r["ledger"], f"new {profit:+.5f} XRP ({r['profit_pct']:+.2f}%) " + " → ".join(view["labels"]))
                if st.best_seen is None or profit > st.best_seen["profit"]:
                    st.best_seen = {"labels": view["labels"], "profit": profit, "profit_pct": r["profit_pct"],
                                    "amount_in": view["amount_in"], "ledger": r["ledger"]}
            elif r["type"] == "end":
                live.pop("|".join(r["path"]), None)
                row = {"labels": labels_of(r["path"]), "first_ledger": r["first_ledger"], "last_ledger": r["last_ledger"],
                       "ledgers_alive": r["ledgers_alive"], "best_profit": float(r["best_profit"]),
                       "best_amount_in": float(r["best_amount_in"])}
                st.ended.appendleft(row)
                st._log(r["last_ledger"] + 1, f"ended after {row['ledgers_alive']} ledger(s), best {row['best_profit']:+.5f} XRP: "
                        + " → ".join(row["labels"]))
        st.live = live
        return st


class Handler(BaseHTTPRequestHandler):
    server_version = "xrpl-arb-ui"

    def log_message(self, *args) -> None:  # keep the terminal for the watcher's own lines
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:  # DNS-rebinding guard
            return self._send(403, b"forbidden host", "text/plain")
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(self.server.dashboard.snapshot()).encode(), "application/json")
        self._send(404, b"not found", "text/plain")


def make_server(dashboard: DashboardState, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.dashboard = dashboard
    shown = server.server_address[1]
    server.allowed_hosts = {f"127.0.0.1:{shown}", f"localhost:{shown}"}
    return server


def serve(dashboard: DashboardState, port: int, on_ready=None) -> None:
    server = make_server(dashboard, port)
    if on_ready:
        on_ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
