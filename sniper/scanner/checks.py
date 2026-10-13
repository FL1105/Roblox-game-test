"""Pure risk checks. Each takes already-fetched data and returns findings; no I/O here."""

from __future__ import annotations

from dataclasses import dataclass

from ..pump.events import TOTAL_SUPPLY
from ..pump.store import DeployerProfile
from .models import Finding, Holder, RoundTrip, Severity as S

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass(frozen=True)
class Thresholds:
    """Tunable cut-offs. Defaults are deliberately conservative starting points, not tested truths."""

    # Holder concentration, as % of circulating supply (pools / bonding curve excluded)
    top1_high: float = 30.0
    top1_medium: float = 15.0
    top10_high: float = 80.0
    top10_medium: float = 60.0
    # Token-2022 transfer fee, in basis points (100 bps = 1%)
    fee_critical_bps: int = 2500
    fee_high_bps: int = 500
    # DEX liquidity in USD
    liquidity_high_usd: float = 5_000
    liquidity_medium_usd: float = 15_000
    young_minutes: float = 10
    # Many buys and (almost) no sells in the last hour
    buys_no_sells_min: int = 15
    # Quoted round-trip loss, %
    roundtrip_critical_pct: float = 30.0
    roundtrip_high_pct: float = 10.0
    roundtrip_medium_pct: float = 5.0
    # Creator's own initial buy, % of total supply (pump.fun launch)
    dev_buy_high_pct: float = 20.0
    dev_buy_medium_pct: float = 10.0
    # Deployer history
    serial_launches_high: int = 5   # earlier launches in the last 24h
    serial_launches_medium: int = 2
    fresh_wallet_hours: float = 1.0


def check_mint(account: dict | None, t: Thresholds) -> list[Finding]:
    """`account` is the `value` of getAccountInfo(encoding=jsonParsed) for the mint."""
    if account is None:
        return [Finding("mint", S.CRITICAL, "Mint account does not exist")]
    if account.get("owner") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return [Finding("mint", S.CRITICAL, f"Account is not owned by a token program ({account.get('owner')})")]
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not parsed or parsed.get("type") != "mint":
        return [Finding("mint", S.CRITICAL, "Account is not a parseable token mint")]

    info = parsed.get("info") or {}
    findings: list[Finding] = []
    if info.get("mintAuthority"):
        findings.append(Finding("mint_authority", S.CRITICAL, "Mint authority is not revoked: supply can be inflated at will"))
    if info.get("freezeAuthority"):
        findings.append(Finding("freeze_authority", S.CRITICAL, "Freeze authority is not revoked: your tokens can be frozen (honeypot)"))
    findings += _check_extensions(info.get("extensions") or [], t)
    return findings


def _check_extensions(extensions: list[dict], t: Thresholds) -> list[Finding]:
    findings: list[Finding] = []
    for ext in extensions:
        name = ext.get("extension")
        state = ext.get("state") or {}
        if name == "permanentDelegate" and state.get("delegate"):
            findings.append(Finding("permanent_delegate", S.CRITICAL, f"Permanent delegate {state['delegate']} can move or burn any holder's tokens"))
        elif name == "nonTransferable":
            findings.append(Finding("non_transferable", S.CRITICAL, "Token is non-transferable: it cannot be sold"))
        elif name == "defaultAccountState" and state.get("accountState") == "frozen":
            findings.append(Finding("default_frozen", S.CRITICAL, "New token accounts start frozen"))
        elif name == "transferHook" and state.get("programId"):
            findings.append(Finding("transfer_hook", S.HIGH, f"Transfer hook program {state['programId']} runs on every transfer and can block sells"))
        elif name == "pausableConfig" and state.get("authority"):
            findings.append(Finding("pausable", S.HIGH, "An authority can pause all transfers"))
        elif name == "transferFeeConfig":
            findings.append(_transfer_fee_finding(state, t))
    return [f for f in findings if f is not None]


def _transfer_fee_finding(state: dict, t: Thresholds) -> Finding | None:
    bps = max(
        int((state.get(k) or {}).get("transferFeeBasisPoints", 0))
        for k in ("newerTransferFee", "olderTransferFee")
    )
    authority = state.get("transferFeeConfigAuthority")
    pct = bps / 100
    if bps >= t.fee_critical_bps:
        return Finding("transfer_fee", S.CRITICAL, f"Transfer fee is {pct:g}%: effectively a honeypot")
    if bps >= t.fee_high_bps:
        return Finding("transfer_fee", S.HIGH, f"Transfer fee is {pct:g}% on every transfer")
    if bps > 0:
        return Finding("transfer_fee", S.MEDIUM, f"Transfer fee is {pct:g}% on every transfer")
    if authority:
        return Finding("transfer_fee", S.MEDIUM, "Transfer fee is 0% now but its authority can raise it")
    return None


def check_holders(holders: list[Holder], supply: int, t: Thresholds) -> list[Finding]:
    """Concentration among real holders. Only the largest ~20 token accounts are visible."""
    excluded = sum(h.amount for h in holders if h.label)
    circulating = supply - excluded
    if supply <= 0 or circulating <= 0:
        return [Finding("holders", S.INFO, "Nothing outside pools/bonding curve in the visible top holders")]

    by_owner: dict[str, int] = {}
    for h in holders:
        if not h.label:
            by_owner[h.owner] = by_owner.get(h.owner, 0) + h.amount
    ranked = sorted(by_owner.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return [Finding("holders", S.INFO, "Nothing outside pools/bonding curve in the visible top holders")]

    top1 = ranked[0][1] / circulating * 100
    top10 = sum(a for _, a in ranked[:10]) / circulating * 100
    summary = (
        f"Top holder {ranked[0][0][:8]}… holds {top1:.1f}% of circulating supply; "
        f"top 10 hold {top10:.1f}% ({excluded / supply * 100:.0f}% of supply sits in pools/curve)"
    )

    findings = [Finding("holders", S.INFO, summary)]
    if top1 >= t.top1_high:
        findings.append(Finding("top_holder", S.HIGH, f"Single wallet holds {top1:.1f}% of circulating supply"))
    elif top1 >= t.top1_medium:
        findings.append(Finding("top_holder", S.MEDIUM, f"Single wallet holds {top1:.1f}% of circulating supply"))
    if top10 >= t.top10_high:
        findings.append(Finding("top10_holders", S.HIGH, f"Top 10 wallets hold {top10:.1f}% of circulating supply"))
    elif top10 >= t.top10_medium:
        findings.append(Finding("top10_holders", S.MEDIUM, f"Top 10 wallets hold {top10:.1f}% of circulating supply"))
    return findings


def check_market(pairs: list[dict] | None, now_ms: int, t: Thresholds) -> list[Finding]:
    """`pairs` is the DexScreener `pairs` list for the token."""
    if not pairs:
        return [Finding("market", S.INFO, "No DEX pair listed (likely still on a bonding curve); market checks skipped")]

    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    findings: list[Finding] = []

    liq = (best.get("liquidity") or {}).get("usd")
    if liq is None:
        findings.append(Finding("liquidity", S.MEDIUM, "Liquidity is unknown"))
    elif liq < t.liquidity_high_usd:
        findings.append(Finding("liquidity", S.HIGH, f"Liquidity is only ${liq:,.0f}"))
    elif liq < t.liquidity_medium_usd:
        findings.append(Finding("liquidity", S.MEDIUM, f"Liquidity is low (${liq:,.0f})"))
    else:
        findings.append(Finding("liquidity", S.INFO, f"Liquidity ${liq:,.0f}"))

    created = best.get("pairCreatedAt")
    if created:
        age_min = (now_ms - created) / 60_000
        if age_min < t.young_minutes:
            findings.append(Finding("age", S.LOW, f"Pair is only {age_min:.0f} min old"))

    h1 = (best.get("txns") or {}).get("h1") or {}
    buys, sells = int(h1.get("buys", 0)), int(h1.get("sells", 0))
    if buys >= t.buys_no_sells_min and sells == 0:
        findings.append(Finding("sell_activity", S.HIGH, f"{buys} buys and 0 sells in the last hour: possible honeypot"))
    elif buys >= 2 * t.buys_no_sells_min and sells / buys < 0.1:
        findings.append(Finding("sell_activity", S.MEDIUM, f"Only {sells} sells against {buys} buys in the last hour"))
    return findings


def check_roundtrip(rt: RoundTrip, t: Thresholds) -> list[Finding]:
    if rt.tokens_out is None:
        return [Finding("roundtrip", S.INFO, f"No buy route found, so sell-ability is unverified ({rt.buy_error or 'no quote'})")]
    if rt.sol_back is None:
        return [Finding("roundtrip", S.CRITICAL, f"A buy route exists but no sell route does ({rt.sell_error or 'no quote'}): likely honeypot")]

    loss = (1 - rt.sol_back / rt.in_lamports) * 100
    msg = f"Quoted round trip of {rt.in_lamports / 1e9:g} SOL loses {loss:.1f}% (fees, price impact, any sell tax)"
    if loss >= t.roundtrip_critical_pct:
        return [Finding("roundtrip", S.CRITICAL, msg)]
    if loss >= t.roundtrip_high_pct:
        return [Finding("roundtrip", S.HIGH, msg)]
    if loss >= t.roundtrip_medium_pct:
        return [Finding("roundtrip", S.MEDIUM, msg)]
    return [Finding("roundtrip", S.INFO, msg)]


def check_launch(dev_tokens: float, t: Thresholds) -> list[Finding]:
    """The creator's own buy in the create transaction, as % of the fixed 1B supply."""
    pct = dev_tokens / TOTAL_SUPPLY * 100
    if pct >= t.dev_buy_high_pct:
        return [Finding("dev_buy", S.HIGH, f"Creator bought {pct:.1f}% of supply at launch")]
    if pct >= t.dev_buy_medium_pct:
        return [Finding("dev_buy", S.MEDIUM, f"Creator bought {pct:.1f}% of supply at launch")]
    return [Finding("dev_buy", S.INFO, f"Creator bought {pct:.1f}% of supply at launch")]


def check_deployer(p: DeployerProfile, t: Thresholds) -> list[Finding]:
    """Reputation of the creator wallet. History only covers launches this tool has recorded."""
    findings: list[Finding] = []
    if p.rugs_total >= 2:
        findings.append(Finding("deployer_rugs", S.CRITICAL, f"Creator has {p.rugs_total} earlier launches where they dumped and the price collapsed"))
    elif p.rugs_total == 1:
        findings.append(Finding("deployer_rugs", S.HIGH, "Creator has 1 earlier launch where they dumped and the price collapsed"))

    if p.launches_24h >= t.serial_launches_high:
        findings.append(Finding("deployer_serial", S.HIGH, f"Creator launched {p.launches_24h} other tokens in the last 24h"))
    elif p.launches_24h >= t.serial_launches_medium:
        findings.append(Finding("deployer_serial", S.MEDIUM, f"Creator launched {p.launches_24h} other tokens in the last 24h"))

    if p.wallet_age_hours is not None and not p.wallet_age_is_lower_bound and p.wallet_age_hours < t.fresh_wallet_hours:
        findings.append(Finding("deployer_wallet", S.MEDIUM, f"Creator wallet is only {p.wallet_age_hours * 60:.0f} min old"))

    if not findings:
        known = f"{p.launches_total} earlier launches seen" if p.launches_total else "no earlier launches seen"
        findings.append(Finding("deployer", S.INFO, f"No deployer red flags ({known}; history is limited to what this tool recorded)"))
    return findings
