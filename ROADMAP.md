# Roadmap

Status key: DONE / NOW / NEXT / NEEDS YOU / ONLY IF EARNED

## Built
1. DONE  Token scanner and deployer-history checks
2. DONE  Paper-trading engine with measured fees (1.238% per side) and per-transaction cost
3. DONE  Terminal: config screen, presets, no-install browser build verified against the Python engine
4. DONE  Real data: PumpPortal launches + exact curve state from one Jupiter quote (k is invariant); mayhem-mode and off-curve tokens excluded
5. DONE  Decision system: path features, smart-exit rules, learned hold/sell model, confirm-entry mode (Python and browser versions agree on every path tested)
6. DONE  Entry filters (min liquidity, dev holding, blacklist, creator history, token age, anti-chasing, min trades) and a filter lab that selects on early launches and confirms on unseen ones
7. DONE  Take-profit ladders with partial sells and a breakeven stop
8. DONE  Real recording: 100 minutes, 1,301 launches seen, 1,154 priceable (data/snapshots/raw_main_final.jsonl.gz)
9. DONE  Final evaluation on unseen launches (data/report_final.md): see "What the data says" below
10. DONE Browser page replays the real recording (looping in marked repeat laps, so the bot never runs out of tokens), with a limit on tokens shown
11. DONE Auto-tune: clones of the bot trade the same tokens with nudged settings; after each round the bot's settings move part of the way toward clones that beat it by more than luck (paired bootstrap, tightened for the number of clones); pauses on repeated data

## What the data says (final recording, 347 unseen launches)
- Every strategy loses on unseen launches, even at zero transaction cost: fixed targets -4.7%, smart exit -4.5%, ladder -4.6%, learned model -3.6% per trade (95% intervals entirely below zero). A round trip costs about 3.1% in fees alone.
- Only 2% of launches ever reach +30% after the first quote (median 13 s after launch); of the tokens that hit either +30% or -15%, 15% hit +30% first. A 30/-15 rule needs 33%+.
- The learned model ranks well (test AUC 0.80 up / 0.91 down) but ranking is not profit: the entry itself loses more than any exit recovers.
- Entry filters: nothing held up on unseen launches. 24 settings were tried, so some looked good early by chance and did not survive.
- Auto-tune on the same data walks to "trade almost nothing" (it raises min trades until the bot stops buying): on this market, not trading beats trading. That is the honest answer, not a bug.
- Limits: the recorder cannot see who traded and sees tokens ~13 s after launch, so this says nothing about faster entries or wallet-following.

## Next
11b. READY  Trade-stream recorder (`python -m sniper.live --stream`): every launch AND every trade with its wallet from second zero, no Jupiter delay. PumpPortal serves this feed only to an API key funded with at least 0.02 SOL (checked live: it answers "only available when connecting with an API key funded with at least 0.02 SOL"). Needs you: create that key with a throwaway wallet, keep the balance tiny, store it as the environment secret PUMPPORTAL_API_KEY (never in chat). The recorder only reads; it has no code that can send a transaction.
12. NEXT  More data (days, not 100 minutes) on a small hosted server: the only way to tell a thin edge from noise
13. NEEDS YOU  Allow api.mainnet-beta.solana.com: shows WHO trades (creator-sell exits, wallet reputation) and real launch-time speed. This is where an edge could still exist
14. NEXT  Re-test any promising idea on fresh, never-seen data (the real proof)

## Optional, need decisions
15. NEEDS YOU  Allow api.mainnet-beta.solana.com: shows WHO trades (creator-sell exits, wallet reputation) and real launch-time speed
16. ONLY IF EARNED  Live trading, only if step 14 shows an edge after costs: separate small wallet, hard loss cap, kill switch, transaction simulation, small pilot. Not started and will not start without an explicit go-ahead
17. LAST  Cleanup, plain-English guide, handover

## Known limits to keep in mind
- The recorder sees how the curve moved, not who moved it.
- Entries happen several seconds after launch; a faster bot would see earlier prices.
- Evidence so far (1,154 launches): blind sniping loses even with zero transaction cost, and nothing tried has beaten costs on unseen launches.
- The engine keeps the full price history of every token it has seen; a server that runs for days will need that pruned (not done yet).

## Other projects (separate from the Solana work above)

### XRPL arbitrage bot (`xrpl_arb/`)
- DONE   Scanner, pool+book liquidity model (checked against the ledger's own `simulate`), shared leg-by-leg trade policy, paper executor, live dashboard, opt-in live executor (not run with real funds), 75 tests. See `xrpl_arb/README.md`.
- DONE   First real-data run: 173 mainnet ledgers, 1,720 cycle evaluations, none profitable, best edge -1.4 bps (XRP -> RLUSD -> XRP).
- DONE   History replay (`python -m xrpl_arb.history`). Last 24 h of mainnet, 740 sampled ledgers (148 windows): no cycle was ever profitable. Closest was XRP -> RLUSD -> XRP: median -10.8 bps, 90th percentile -6.8, best -0.0. Every other cycle was at least -26 bps. Zero paper trades. Sampling can miss a gap lasting only 1-2 ledgers.
- NEXT   Longer replay (72 h) and a contiguous read (every ledger) of any volatile hour, since sampling can miss short gaps.
- NEXT   Record every ledger's best edges to a file for long unattended runs.
- NEEDS YOU  Live trading stays off until a replay or a long paper run shows an edge that survives the late-leg model.

### Parked ideas
- IDEA (2026-10-21, not started)  Market making on Polymarket's 5-minute Bitcoin Up/Down markets. Source: a tweet describing the wallet `norm1e69`. It is market making, not arbitrage.
  - What the public trade data shows (last ~11,000 fills, 778 markets): quotes follow the price (only 7.5% of fills at 46-50c, 28% of dollars at 80c+), both sides bought in 84% of markets, average Up + Down price 0.95 (median 0.98, under $1.00 in 60% of markets), ~15% of shares left unmatched (directional risk), median fill $12. The tweet's "static 48c/48c" is a simplification.
  - Profit claim: plausible, not verified. Polymarket's leaderboard shows +$34.7k over 7 days on $4.6M volume and +$179k over 30 days (about 0.8-0.9% of volume); its all-time figure ($49.9k) contradicts its own 30-day figure, so $315k and "73 green days of 74" are unconfirmed.
  - Why it can work: the markets use a taker-only fee (rate 0.07) with a 20% maker rebate, plus liquidity rewards (quotes within 4.5c, min 50 shares). Risks: adverse selection when BTC trends, thin margin, competition, real money.
  - Reproduce: `gamma-api.polymarket.com/public-search?q=<name>` gives the wallet; `data-api.polymarket.com/v1/leaderboard?user=<wallet>&timePeriod=WEEK|MONTH|ALL`, `/trades?user=<wallet>&limit=1000&offset=N` (history depth stops near offset 11,000), `gamma-api.polymarket.com/markets?slug=<slug>` for the fee schedule. Send a browser User-Agent.
  - DONE (2026-10-21) `poly_mm/`: tape backtest of a paper market maker, 865 resolved 5-minute markets, 1.27M trades, train/test split. Every variant loses on the held-out half: the tweet's static 0.48/0.48 -14% of volume, following the last price -5% to -7%, quoting off one-minute BTC candles -14% to -20%. Trending markets fill only the losing side. This tape-level simulator cannot reproduce the wallet's +0.8%, so whatever earns it (speed, information, cancelling, rebates, rewards) is not in public tape data. See `poly_mm/README.md`.
  - NEXT: cross-venue arbitrage (Polymarket vs Kalshi hourly BTC) from the reference repo CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot: real asks, fees, depth, and the settlement-source mismatch (Chainlink vs Kalshi's benchmark) that makes it not risk-free near the strikes. Kalshi's public API is reachable.
  - NEEDS YOU: check Polymarket is available in your jurisdiction and its terms before any step that touches money; go-ahead to build the recorder.

