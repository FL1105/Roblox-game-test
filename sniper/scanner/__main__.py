"""Usage: python -m sniper.scanner <mint> [<mint> ...] [--json]"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .scan import scan_token
from .sources import DexScreener, JupiterQuotes, SolanaRpc

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
ORDER = ["critical", "high", "medium", "low", "info"]


def render(result) -> str:
    lines = [f"{result.mint}", f"  verdict: {result.verdict.value}  (risk score {result.score})"]
    unchecked = [s for s, ok in result.sources_ok.items() if not ok]
    if unchecked:
        lines.append(f"  unchecked sources: {', '.join(unchecked)}")
    for f in sorted(result.findings, key=lambda f: ORDER.index(f.severity.value)):
        lines.append(f"  [{f.severity.value.upper():8}] {f.check}: {f.message}")
    if result.verdict.value == "PASS":
        lines.append("  note: PASS means these checks found nothing, not that the token is safe.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sniper.scanner", description="Read-only Solana token risk scanner")
    p.add_argument("mints", nargs="+", help="token mint addresses")
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC), help="RPC URL (default: $SOLANA_RPC_URL or public mainnet)")
    p.add_argument("--trade-sol", type=float, default=0.1, help="size used for the quoted round-trip check")
    p.add_argument("--no-market", action="store_true", help="skip DexScreener checks")
    p.add_argument("--no-route", action="store_true", help="skip Jupiter round-trip checks")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    rpc = SolanaRpc(args.rpc)
    dex = None if args.no_market else DexScreener()
    jup = None if args.no_route else JupiterQuotes()

    results = []
    for mint in args.mints:
        try:
            results.append(scan_token(mint, rpc, dex, jup, trade_lamports=int(args.trade_sol * 1e9)))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print("\n\n".join(render(r) for r in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
