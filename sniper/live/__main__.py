"""Usage: python -m sniper.live --out data/raw.jsonl --minutes 120

Records real launches and their price paths (needs `pip install websockets`). Read-only.
"""

import argparse
import asyncio

from .collector import run


def main() -> int:
    p = argparse.ArgumentParser(prog="sniper.live", description="Record real pump.fun launches and price paths")
    p.add_argument("--out", default="raw_launches.jsonl")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--rps", type=float, default=12.0, help="max Jupiter requests per second")
    p.add_argument("--observe-seconds", type=float, default=600)
    a = p.parse_args()
    try:
        asyncio.run(run(a.out, a.minutes, a.rps, a.observe_seconds))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
