from __future__ import annotations

import re
import time

from .checks import Thresholds, check_holders, check_market, check_mint, check_roundtrip
from .models import Finding, Holder, ScanResult, Severity
from .sources import DexScreener, JupiterQuotes, SolanaRpc, SourceError

_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Program IDs whose accounts hold a token's liquidity rather than a "real" holder's bag.
LIQUIDITY_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "bonding_curve",  # pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pool",           # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "pool",          # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "pool",          # Raydium CPMM
}
# Raydium AMM v4 vaults are owned by this PDA, which has no account of its own.
LIQUIDITY_AUTHORITIES = {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "pool"}


def is_valid_mint(address: str) -> bool:
    return bool(_BASE58.match(address))


def resolve_holders(rpc: SolanaRpc, mint: str) -> list[Holder]:
    """Top token accounts -> owning wallets, labelling pump.fun curves and AMM pools."""
    largest = rpc.get_largest_accounts(mint)
    if not largest:
        return []
    accounts = rpc.get_multiple_accounts([a["address"] for a in largest])

    owners: list[str | None] = []
    for acc in accounts:
        data = acc.get("data") if acc else None
        info = ((data or {}).get("parsed") or {}).get("info") if isinstance(data, dict) else None
        owners.append((info or {}).get("owner"))

    unique = sorted({o for o in owners if o})
    owner_accounts = dict(zip(unique, rpc.get_multiple_accounts(unique))) if unique else {}

    holders = []
    for entry, owner in zip(largest, owners):
        if not owner:
            continue
        label = LIQUIDITY_AUTHORITIES.get(owner)
        owner_acc = owner_accounts.get(owner)
        if label is None and owner_acc:
            label = LIQUIDITY_PROGRAMS.get(owner_acc.get("owner"))
        holders.append(Holder(owner, int(entry["amount"]), label))
    return holders


def scan_token(
    mint: str,
    rpc: SolanaRpc,
    dex: DexScreener | None = None,
    jupiter: JupiterQuotes | None = None,
    *,
    thresholds: Thresholds | None = None,
    trade_lamports: int = 100_000_000,
    now_ms: int | None = None,
) -> ScanResult:
    """Run every available check. A failing data source never yields a clean result:
    it is recorded in `sources_ok` and surfaces as a finding."""
    if not is_valid_mint(mint):
        raise ValueError(f"not a valid Solana address: {mint!r}")
    t = thresholds or Thresholds()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    result = ScanResult(mint)

    def run(name: str, fn):
        try:
            result.findings.extend(fn())
            result.sources_ok[name] = True
        except SourceError as e:
            result.sources_ok[name] = False
            result.findings.append(Finding(name, Severity.MEDIUM, f"Could not run {name} checks: {e}"))

    run("mint", lambda: check_mint(rpc.get_account(mint), t))
    if any(f.severity is Severity.CRITICAL and f.check == "mint" for f in result.findings):
        return result  # not a real token mint; nothing else is meaningful

    run("holders", lambda: check_holders(resolve_holders(rpc, mint), rpc.get_token_supply(mint), t))
    if dex:
        run("market", lambda: check_market(dex.pairs(mint), now_ms, t))
    if jupiter:
        run("roundtrip", lambda: check_roundtrip(jupiter.round_trip(mint, trade_lamports), t))
    return result
