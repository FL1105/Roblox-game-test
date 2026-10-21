"""Read-only access to Polymarket's public HTTP APIs (standard library only). Nothing here can place an order."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = "Mozilla/5.0 (poly_mm read-only research script)"  # the APIs refuse the default Python agent


class ApiError(Exception):
    pass


def get_json(url: str, params: dict | None = None, retries: int = 4, timeout: float = 30.0):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):  # the request itself is wrong or there is nothing there: retrying will not help
                raise ApiError(f"{e.code} for {url}")
            last = e
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
        time.sleep(0.5 * 2 ** attempt)
    raise ApiError(f"{url}: {last}")
