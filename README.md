# Solana / pump.fun research toolkit

Four read-only tools that build on each other. **None of them holds a wallet, a key, or sends a transaction.**
Python 3.10+, standard library only (the live listener also needs `websockets`).

```
python -m unittest discover -s tests -t .            # 127 tests, no network needed
python -m sniper.scanner <mint> [--json]             # 1. scan one token
python -m sniper.listener --events launches.jsonl    # 2. record live launches + verdicts
python -m sniper.paper replay launches.jsonl         # 3. paper-trade the recording
python -m sniper.paper sweep  launches.jsonl         #    tune on half, validate on the other half
python -m sniper.terminal                            # 4. dashboard in your browser (demo market)
```

Windows: double-click `start_terminal.bat` (needs Python 3.10+ installed).

Set `SOLANA_RPC_URL` to a private RPC (Helius, QuickNode, ...). The public endpoint rate-limits fast.
Install the listener's one dependency with `pip install -r requirements-live.txt`.

## The terminal (`sniper/terminal`)

A browser dashboard (Dashboard / Logs / Profile) over the same paper engine. It serves on `127.0.0.1` only.

```
python -m sniper.terminal                        # SIMULATED demo market: for trying the UI, proves nothing
python -m sniper.terminal --replay launches.jsonl --speed 20
python -m sniper.terminal --live --rpc URL       # real launches, still paper trading (needs websockets)
```

- Start/Stop gates new entries (open positions are still managed). Amount range sizes PASS trades at the
  top and CAUTION trades at the bottom. Settings apply live and are validated server-side.
- There is **no wallet connection, key field or order-sending code**, and tests assert the page never asks for one.
- The server refuses foreign `Host` headers and non-JSON POSTs, and the page renders chain data (token names)
  with `textContent` only, since token names are attacker-controlled.
- Demo data is synthetic; its P&L and scanner accuracy are invented.

## Suggested workflow

1. **First run on real tokens (do this before anything else).** Scan five or six pump.fun mints with
   `python -m sniper.scanner`. If *every* pump.fun token is REJECTed for `mint_authority`, my assumption
   that pump.fun revokes it is wrong; send me the authority address so it can be allow-listed.
2. **Record for a few days** with the listener. It logs every launch, its trades for 10 minutes, and the
   scanner's verdict at the time. Unfiltered logging is what keeps the later test free of survivorship bias.
3. **Replay** the log. The report compares your filtered strategy against *buy every launch* with the same
   exits and costs; if the filter doesn't beat that baseline, it isn't doing anything.
4. **Sweep** take-profit/stop-loss on the first half of the data and look at the second half. A setting only
   counts if the TEST column is positive with a healthy trade count. With fewer than 20 trades the report
   refuses to quote a confidence interval.

## Scanner checks (`sniper/scanner`)

| Check | Source | Why it matters |
|---|---|---|
| Mint / freeze authority not revoked | RPC | Unlimited minting; frozen holders (honeypot) |
| Token-2022: permanent delegate, non-transferable, default-frozen, transfer hook, pausable, transfer fee | RPC | Ways to seize tokens, block sells or tax exits |
| Holder concentration (pump.fun curve and AMM pools excluded) | RPC | A few wallets holding the supply can rug |
| Creator's initial buy as % of supply | launch event | Large dev bags get dumped |
| Deployer history: earlier launches in 24h, earlier rugs, wallet age | local store + RPC | Serial launchers and repeat ruggers |
| Liquidity, pair age, buys vs sells | DexScreener | Thin liquidity, buys with no sells |
| Quoted buy-then-sell round trip | Jupiter | No sell route, or a large hidden loss |

Verdicts: `REJECT` (any critical finding), `AVOID`, `CAUTION`, `PASS`, `UNKNOWN`. If the mint or holder data
can't be fetched the verdict is `UNKNOWN`, never `PASS`. RPC reads use `confirmed` commitment, and the
listener retries a few times before believing a brand-new mint "does not exist".

## How the paper trader models reality (`sniper/paper`)

- A decision at time T fills at the curve state at **T + latency** (default 800 ms): price can move against you.
- Entry and exit each pay the **pump fee** (default 1%, verify the current rate) and a flat **per-tx cost**
  (default 0.003 SOL priority fee/tip). At 0.1 SOL trades the tx costs alone are ~6% of the position, on top of ~2% in pump fees.
- Take-profit and stop-loss are measured **net of costs**. At 0.1 SOL a 30% net target needs roughly a 40% price move.
- Exits on: take-profit, stop-loss, max hold, **creator selling**, or the curve graduating.
- Position size is fixed or a fraction of equity (`--size-frac`) for compounding.

**It does not model:** other bots landing in the same slot ahead of you, dropped/failed transactions, selling
after graduation, or your own price impact. Real results will be worse than paper results, so use paper to
*rule strategies out*, not to promise yourself a return.

## Limits and unverified assumptions

- `PASS` is "no red flags found", not "safe". Delayed honeypots, custom-program blacklists and coordinated
  insider wallets are not detected.
- Deployer history only knows creators the listener has *itself* recorded since you started, so it is weak
  at first and improves with time. A "rug" is defined as the creator selling at least half of what they bought
  *and* the price ending at least 50% below its peak inside the observation window (`pump/store.py`).
- **Not verified against live services** (my sandbox blocks them): the PumpPortal message field names
  (`pump/events.py`), the pump.fun / PumpSwap / Raydium program IDs (`scanner/scan.py`), the bonding-curve
  constants and fee rate (`pump/curve.py`). Parsing is tolerant, so a mismatch shows up as "nothing recorded"
  rather than wrong data, but check the first few lines of your JSONL.
- Thresholds in `checks.Thresholds` and `paper.Strategy` are starting points, not tested truths.
