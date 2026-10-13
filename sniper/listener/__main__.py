"""Usage: python -m sniper.listener --events launches.jsonl --db launches.db [--rpc URL]

Record-only: no wallet, no keys, no trading.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from ..pump.store import LaunchStore
from ..scanner.scan import scan_token, with_wallet_activity
from ..scanner.sources import SolanaRpc, SourceError
from .live import JsonlSink, Listener, connect_and_run

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


def make_scan_fn(rpc: SolanaRpc, store: LaunchStore, wallet_lookup: bool, retries: int = 3, retry_delay_s: float = 0.7):
    """Scan one fresh launch. A brand-new mint can be invisible to the RPC for a moment, so a
    'does not exist' result is retried a few times before it is believed."""

    def scan(launch):
        profile = store.profile(launch.creator, launch.t, exclude_mint=launch.mint)
        if wallet_lookup:
            try:
                profile = with_wallet_activity(rpc, launch.creator, profile, time.time())
            except SourceError:
                pass  # leave wallet fields unset; the scan still runs
        for attempt in range(retries + 1):
            result = scan_token(launch.mint, rpc, deployer=profile, dev_tokens=launch.dev_tokens)
            missing = any(f.check == "mint" and "does not exist" in f.message for f in result.findings)
            if not missing or attempt == retries:
                break
            time.sleep(retry_delay_s)
        notable = [f"{f.severity.value}:{f.check}" for f in result.findings if f.severity.value in ("critical", "high", "medium")]
        return result.verdict.value, result.score, ",".join(notable)

    return scan


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sniper.listener", description="Record pump.fun launches, trades and scanner verdicts")
    p.add_argument("--events", default="launches.jsonl", help="JSONL log to append to")
    p.add_argument("--db", default="launches.db", help="SQLite file for deployer history")
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC))
    p.add_argument("--observe-seconds", type=float, default=600, help="how long to follow each token's trades")
    p.add_argument("--max-scans-per-min", type=int, default=30, help="RPC budget guard")
    p.add_argument("--no-wallet-lookup", action="store_true", help="skip the per-creator wallet-age RPC call")
    a = p.parse_args(argv)

    store = LaunchStore(a.db)
    sink = JsonlSink(a.events)
    listener = Listener(
        sink, store, make_scan_fn(SolanaRpc(a.rpc), store, not a.no_wallet_lookup),
        observe_s=a.observe_seconds, max_scans_per_min=a.max_scans_per_min,
    )

    async def runner():
        try:
            await connect_and_run(listener)
        finally:
            await listener.drain()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    finally:
        sink.close()
        store.close()
        print(f"stopped. {listener.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
