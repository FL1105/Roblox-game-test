"""Read-only client for Kalshi's public market-data API, plus an on-disk cache. No key, no orders.

Kalshi has renamed price fields and moved older markets to /historical/* before, so parsing is defensive
(prices arrive as integer cents or as "0.5600" dollar strings) and `python -m kalshi_btc15 probe` prints the
raw responses so the assumptions can be checked against the live API.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"
CACHE = Path(__file__).with_name("cache")


class ApiError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


MIN_INTERVAL = 0.12  # seconds between requests, shared by all threads; roughly 8 requests a second
_gate = {"lock": threading.Lock(), "next": 0.0, "interval": MIN_INTERVAL}


def _wait_turn() -> None:
    with _gate["lock"]:
        now = time.monotonic()
        wait = max(0.0, _gate["next"] - now)
        _gate["next"] = max(now, _gate["next"]) + _gate["interval"]
    if wait:
        time.sleep(wait)


def _adapt(rate_limited: bool) -> None:
    """Halve the request rate (down to one every 2 s) after a 429; creep back towards MIN_INTERVAL on success."""
    with _gate["lock"]:
        _gate["interval"] = min(2.0, _gate["interval"] * 2) if rate_limited else max(MIN_INTERVAL, _gate["interval"] * 0.97)


def get_json(url: str, params: dict | None = None, retries: int = 8) -> dict | list:
    """GET with a shared rate limit; retries on 429 / 5xx / network errors, other HTTP errors raise ApiError."""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    last = None
    for attempt in range(retries):
        _wait_turn()
        try:
            req = urllib.request.Request(full, headers={"Accept": "application/json", "User-Agent": "kalshi-btc15-research/1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.load(resp)
            _adapt(False)
            return body
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise ApiError(f"HTTP {e.code} for {full}", e.code) from e
            last = e
            _adapt(e.code == 429)
            retry_after = e.headers.get("Retry-After") if e.headers else None
            time.sleep(float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else min(30.0, 1.5 ** attempt))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(min(30.0, 1.5 ** attempt))
    raise ApiError(f"gave up after {retries} tries on {full}: {last}")


def cents(node, key: str) -> float | None:
    """A price field as float cents. Three shapes occur: {key + "_dollars": "0.5600"} (live), {key: "0.5600"}
    (historical endpoints: a dollar string under the plain key) and {key: 56} (integer cents). None when absent."""
    if not isinstance(node, dict):
        return None
    dollars = node.get(key + "_dollars")
    if dollars not in (None, ""):
        return float(dollars) * 100.0
    raw = node.get(key)
    if raw in (None, ""):
        return None
    return float(raw) * 100.0 if isinstance(raw, str) else float(raw)


def iso_ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


@dataclass(frozen=True)
class Market:
    ticker: str
    close_ts: int
    yes_won: bool
    strike: float | None


@dataclass(frozen=True)
class Candle:
    end_ts: int
    bid: float | None   # YES bid at the close of the period, cents
    ask: float | None   # YES ask at the close of the period, cents
    last: float | None  # last traded YES price, cents
    volume: float


def parse_market(raw: dict) -> Market | None:
    """None for markets that are not settled yes/no or lack a close time."""
    if raw.get("result") not in ("yes", "no") or not raw.get("close_time"):
        return None
    strike = raw.get("floor_strike", raw.get("floor_strike_dollars"))
    return Market(raw["ticker"], iso_ts(raw["close_time"]), raw["result"] == "yes", None if strike in (None, "") else float(strike))


def parse_candle(raw: dict) -> Candle:
    volume = raw.get("volume", raw.get("volume_fp")) or 0
    return Candle(int(raw["end_period_ts"]), cents(raw.get("yes_bid"), "close"), cents(raw.get("yes_ask"), "close"),
                  cents(raw.get("price"), "close"), float(volume))


def read_cache(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_cache(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def series_info(series: str = SERIES) -> dict:
    return get_json(f"{BASE}/series/{series}").get("series", {})


def settled_markets(series: str = SERIES, max_pages: int = 80, refresh: bool = False) -> list[Market]:
    """Every settled market of a series, from the live and the historical endpoints, oldest first.

    Only series_ticker / status / limit / cursor are sent: no timestamp filters, whose compatibility with
    `status` has differed between API versions. The caller filters on close time.
    """
    path = CACHE / f"markets_{series}.json"
    raw = None if refresh else read_cache(path)
    if raw is None:
        raw, seen = [], set()
        for endpoint in ("/markets", "/historical/markets"):
            cursor = ""
            for _ in range(max_pages):
                params = {"series_ticker": series, "limit": 1000}
                if endpoint == "/markets":
                    params["status"] = "settled"
                if cursor:
                    params["cursor"] = cursor
                try:
                    body = get_json(BASE + endpoint, params)
                except ApiError as e:
                    if endpoint == "/historical/markets" and e.code in (400, 404):
                        break  # no historical partition for this series / API version
                    raise
                for m in body.get("markets", []):
                    if m["ticker"] not in seen:
                        seen.add(m["ticker"])
                        raw.append(m)
                cursor = body.get("cursor") or ""
                if not cursor:
                    break
        write_cache(path, raw)
    markets = [m for m in map(parse_market, raw) if m]
    return sorted(markets, key=lambda m: m.close_ts)


def candles(series: str, market: Market, window_s: int = 900, period_min: int = 1) -> list[Candle]:
    """1-minute candles covering the whole 15-minute window of one market (cached)."""
    path = CACHE / "candles" / f"{market.ticker}.json"
    raw = read_cache(path)
    if raw is None:
        params = {"start_ts": market.close_ts - window_s - 60, "end_ts": market.close_ts + 60, "period_interval": period_min}
        try:
            body = get_json(f"{BASE}/series/{series}/markets/{market.ticker}/candlesticks", params)
        except ApiError as e:
            if e.code not in (400, 404):
                raise
            body = get_json(f"{BASE}/historical/markets/{market.ticker}/candlesticks", params)
        raw = body.get("candlesticks", [])
        write_cache(path, raw)
    return [parse_candle(c) for c in raw]


def _candle_path(ticker: str) -> Path:
    return CACHE / "candles" / f"{ticker}.json"


def candle_batches(markets: list[Market], budget: int = 9000, window_s: int = 900) -> list[list[Market]]:
    """Group markets (by close time) so each batch stays under the endpoint's cap of 10,000 requested candles.

    The cap counts markets x minutes in the shared time range, so consecutive 15-minute markets fit about 20 a call.
    """
    batches: list[list[Market]] = []
    for m in sorted(markets, key=lambda x: x.close_ts):
        if batches:
            span_min = (m.close_ts - batches[-1][0].close_ts + window_s + 120) / 60
            if (len(batches[-1]) + 1) * span_min <= budget:
                batches[-1].append(m)
                continue
        batches.append([m])
    return batches


def historical_cutoff() -> int:
    """Markets that settled before this time are served by /historical/*; the batch candle call returns nothing for them."""
    try:
        return iso_ts(get_json(f"{BASE}/historical/cutoff")["market_settled_ts"])
    except (ApiError, KeyError, ValueError):
        return 0


def prefetch_candles(series: str, markets: list[Market], workers: int = 6, progress=None) -> None:
    """Download (and cache) candles for every market.

    Recent markets go ~20 to a call via /markets/candlesticks; markets older than the historical cutoff go one
    call each (in parallel, under the shared rate limit). A market the batch call does not return falls back to a
    single call. A market that still fails is left for a later run.
    """
    todo = [m for m in markets if not _candle_path(m.ticker).exists()]
    cutoff = historical_cutoff()
    recent = [m for m in todo if m.close_ts >= cutoff]
    tasks = candle_batches(recent) + [[m] for m in todo if m.close_ts < cutoff]
    failed = done = 0

    def run_task(batch: list[Market]) -> int:
        got: dict[str, list] = {}
        if len(batch) > 1:
            try:
                body = get_json(f"{BASE}/markets/candlesticks", {
                    "market_tickers": ",".join(m.ticker for m in batch), "start_ts": batch[0].close_ts - 960,
                    "end_ts": batch[-1].close_ts + 60, "period_interval": 1})
                got = {row["market_ticker"]: row.get("candlesticks", []) for row in body.get("markets", [])}
            except ApiError:
                pass
        bad = 0
        for m in batch:
            if got.get(m.ticker):
                write_cache(_candle_path(m.ticker), got[m.ticker])
                continue
            try:
                candles(series, m)
            except ApiError:
                bad += 1
        return bad

    with ThreadPoolExecutor(workers) as pool:
        for batch, bad in zip(tasks, pool.map(run_task, tasks)):
            failed += bad
            before, done = done, done + len(batch)
            if progress and done // 1000 > before // 1000:
                progress(done, len(todo), failed)
