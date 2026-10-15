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
