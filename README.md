# Solana token scanner

Read-only risk scanner for Solana tokens (pump.fun and beyond). It never touches a wallet or private key.

```
python -m sniper.scanner <mint> [<mint> ...] [--json] [--rpc URL] [--trade-sol 0.1]
python -m unittest discover -s tests -t .
```

Set `SOLANA_RPC_URL` to a private RPC (Helius, QuickNode, ...). The public endpoint rate-limits quickly.
Python 3.10+, standard library only.

## What it checks

| Check | Source | Why it matters |
|---|---|---|
| Mint authority not revoked | RPC | Dev can mint unlimited supply and dump |
| Freeze authority not revoked | RPC | Dev can freeze your tokens: classic honeypot |
| Token-2022 extensions: permanent delegate, non-transferable, default-frozen, transfer hook, pausable, transfer fee | RPC | Ways to seize tokens, block sells or tax exits |
| Holder concentration (pump.fun curve and AMM pools excluded) | RPC | Few wallets holding most supply can rug |
| Liquidity, pair age, buys vs sells | DexScreener | Thin liquidity, buys with no sells |
| Quoted buy-then-sell round trip | Jupiter | No sell route, or a large hidden loss |

Verdicts: `REJECT` (any critical finding), `AVOID`, `CAUTION`, `PASS`, and `UNKNOWN`.
If the mint or holder data can't be fetched the verdict is `UNKNOWN`, never `PASS`.

## Limits (read these)

- **PASS is not safe.** It means these checks found nothing. Delayed honeypots, blacklists inside custom
  programs, coordinated insider wallets and dev-sells-into-your-buy are not detected.
- The Jupiter check is a **quote**, not a transaction simulation. A real exit test needs a funded wallet
  and `simulateTransaction`; that belongs in the trading bot, not this scanner.
- Not yet checked: deployer history (serial ruggers), token metadata mutability, bundled/sniped launch
  wallets, social links. These need an indexer or a Helius-style API.
- Thresholds in `checks.Thresholds` are untested starting points. Tune them against recorded launches.
- Hard-coded program IDs for pump.fun, PumpSwap and Raydium (`scan.py`) should be verified against current
  docs; new AMMs need adding or their pools will count as "holders".
