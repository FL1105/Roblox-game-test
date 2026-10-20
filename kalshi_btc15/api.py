"""Read-only client for Kalshi's public market-data API, plus an on-disk cache. No key, no orders.

Kalshi has renamed price fields and moved older markets to /historical/* before, so parsing is defensive
(prices arrive as integer cents or as "0.5600" dollar strings) and `python -m kalshi_btc15 probe` prints the
raw responses so the assumptions can be checked against the live API.
"""

from __future__ import annotations

import json
import os
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


def get_json(url: str, params: dict | None = None, retries: int = 6, pause: float = 0.06) -> dict | list:
    """GET with retry on 429 / 5xx / network errors (exponential backoff); other HTTP errors raise ApiError."""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={"Accept": "application/json", "User-Agent": "kalshi-btc15-research/1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.load(resp)
            time.sleep(pause)
            return body
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise ApiError(f"HTTP {e.code} for {full}", e.code) from e
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(min(30.0, 1.5 ** attempt))
    raise ApiError(f"gave up after {retries} tries on {full}: {last}")


def cents(node, key: str) -> float | None:
    """A price field as float cents, from {key: 56} or {key + "_dollars": "0.5600"}; None when absent or null."""
    if not isinstance(node, dict):
        return None
    dollars = node.get(key + "_dollars")
    if dollars not in (None, ""):
        return float(dollars) * 100.0
    raw = node.get(key)
    return None if raw in (None, "") else float(raw)


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


def prefetch_candles(series: str, markets: list[Market], workers: int = 4, progress=None) -> None:
    """Download (and cache) candles for every market. A market that fails is left for a later run."""
    def one(m: Market):
        try:
            candles(series, m)
        except ApiError:
            return m.ticker
        return None

    with ThreadPoolExecutor(workers) as pool:
        failed = 0
        for i, bad in enumerate(pool.map(one, markets), 1):
            failed += bad is not None
            if progress and i % 200 == 0:
                progress(i, len(markets), failed)
