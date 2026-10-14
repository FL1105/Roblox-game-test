# Roadmap

Status key: DONE / NOW / NEXT / NEEDS YOU / ONLY IF EARNED

## Built
1. DONE  Token scanner and deployer-history checks
2. DONE  Paper-trading engine with measured fees (1.238% per side) and per-transaction cost
3. DONE  Terminal: config screen, presets, no-install browser build verified against the Python engine
4. DONE  Real data: PumpPortal launches + exact curve state from one Jupiter quote (k is invariant); mayhem-mode and off-curve tokens excluded
5. DONE  Decision system: path features, smart-exit rules, learned hold/sell model, confirm-entry mode (Python and browser versions agree on every path tested)

## In progress
6. NOW   Recording real launches (running; clean dataset ends about 03:09)
7. NOW   Real-data replay inside the browser page (data source switch, notes, escaping of chain-supplied names)

## Next
8.  NEXT  Convert the recording to events; descriptive analysis
9.  NEXT  Train on the earlier 70% of launches, judge only on the later 30%
10. NEXT  Compare every strategy on the unseen launches at three cost levels, with confidence intervals; say plainly whether any beats costs
11. NEXT  Update the browser link with real data + trained model; save the dataset to the repo
12. NEXT  Only if the numbers justify it: earlier entries, creator/deployer filters, ablation of which smart rules help (tune on training launches only)
13. NEEDS YOU  Collect days of data on a small hosted server (one-command setup from me; you provide and pay for hosting)
14. NEXT  Re-test on fresh, never-seen data after any tuning (the real proof)

## Optional, need decisions
15. NEEDS YOU  Allow api.mainnet-beta.solana.com: shows WHO trades (creator-sell exits, wallet reputation) and real launch-time speed
16. ONLY IF EARNED  Live trading, only if step 14 shows an edge after costs: separate small wallet, hard loss cap, kill switch, transaction simulation, small pilot. Not started and will not start without an explicit go-ahead
17. LAST  Cleanup, plain-English guide, handover

## Known limits to keep in mind
- The recorder sees how the curve moved, not who moved it.
- Entries happen several seconds after launch; a faster bot would see earlier prices.
- Early evidence (a few hundred launches): blind sniping loses even with zero transaction cost.
