# Solana / pump.fun research toolkit

## Easiest way to try it (no install, no Python)

Open **`dist/launch-sniper.html`** in any browser (download that one file from GitHub, then double-click it).
It runs the whole paper-trading terminal by itself on a **simulated** market. It has no server, makes no network
requests and never asks for a wallet or key. It is the same engine as the Python version, rewritten in
JavaScript and checked against it by `tests/test_js_parity.py`. After editing `sniper/terminal/static/`, rebuild
it with `python tools/build_standalone.py` (a test fails if the committed file is stale).

Four read-only tools that build on each other. **None of them holds a wallet, a key, or sends a transaction.**
Python 3.10+, standard library only (the live listener also needs `websockets`).

```
python -m unittest discover -s tests -t .            # 162 tests, no network needed
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

**Bot Configuration** (gear icon, or "Advanced Settings"): Trading / Safety / Advanced tabs, one-click presets
(Steady = the 30% goal, Safe, Balanced, Aggressive, Degen) and a paper bankroll you can set to any amount
(Profile tab), so you can try settings with no deposit. Every control maps to a rule the simulator applies:
take-profit, stop-loss, trailing stop, slippage tolerance (aborts a buy that fills too far from the decision
price), min liquidity (SOL raised on the curve), max dev hold, max concurrent positions, position-size mode
(by verdict / random in range / fixed), auto-compound, wallet blacklist (plus any creator seen dumping),
honeypot detection (require a scanner clearance), rug-pull protection (exit when the creator sells), trading
speed (decision-to-fill delay), max hold time, and risk tolerance (which verdicts may be bought).
The "AI market analysis / smart entry-exit / AI agent" switches from the site that inspired the layout are
deliberately absent: they define no behaviour to test.

**Auto-tune** (Auto-tune tab, `sniper/paper/tuner.py`): while the bot runs, 2 to 8 *clones* of it trade the same tokens on
paper, each with a few settings nudged (take profit, stop loss, trailing stop, max hold, min liquidity, dev hold, min trades,
and the confirm / don't-chase settings when they apply). After every round of N launches each clone is compared with the real
bot launch by launch. A clone only counts if its lower confidence bound (paired bootstrap, tightened for the number of clones
you run) is above zero and it acted differently on at least 5 launches; then the bot's settings move part of the way toward
the winners, weighted by how much each gained, inside fixed limits. Clones are judged only on launches that arrived after
they were created, and tuning pauses on repeated laps of a recording (the bot has already seen that data). It cannot create an
edge that is not there: on the real recording it learns that not trading beats trading. `--loop` makes `--replay` play the
recording again in marked repeat laps.

Browser click test: `python -m sniper.terminal --no-browser &`, then `npm i playwright-core` and
`node e2e/terminal_click_test.js` (see the file header; it changes that instance's settings).

### Recording trades with wallets (needs a key)
`PUMPPORTAL_API_KEY=... python -m sniper.live --stream --out data/raw_stream.jsonl --minutes 240` records every launch and every trade (wallet, side, size,
curve state after it) from the first second, using PumpPortal's data stream, which only serves a key funded with at least 0.02 SOL. The key is used to read:
the program sends subscribe/unsubscribe messages only and contains no code that can send a transaction. The key is read from the environment and scrubbed from
every log line and file. Outages are retried and marked as `gap`/`resume` rows. Stream recordings convert to the same events as the price-only ones, plus the
real trader of each trade (so "creator sold" exits work).

## Real data and the decision system

```
python -m sniper.live --out data/raw.jsonl --minutes 600       # record real launches + exact price paths (read-only)
python -m sniper.live.tools events data/raw.jsonl --out data/events.jsonl
python -m sniper.live.analyze data/raw.jsonl                   # what happens to blindly sniped launches?
python -m sniper.policy.train data/raw.jsonl --out data/model.json   # learn hold/sell on early launches, test on later ones
python -m sniper.paper compare data/events.jsonl --model data/model.json [--tx-cost 0.001]
python -m sniper.terminal --replay data/events.jsonl --model data/model.json
python tools/build_standalone.py --events data/events.jsonl --model data/model.json   # browser build with the real data inside
```

How the real data works (`sniper/live`): PumpPortal announces each launch (with the exact starting reserves) and Jupiter
quotes give real executable prices. A pump.fun curve is constant-product with an invariant `k = SOL x tokens`, so a single
0.1 SOL buy quote pins the exact reserves. A second, larger quote verifies each token follows the standard curve; "mayhem
mode" launches and any off-curve token are excluded because their prices cannot be reconstructed. Measured on live tokens:
the one-quote state predicts quotes at every size from 0.01 to 3 SOL to about 0.02%, and the fee is 1.238% per side.

The decision system (`sniper/policy`): path features (momentum, net SOL flow, activity, drawdown...), explainable smart-exit
rules, a small learned hold/sell model, and a "confirm" entry mode. Every one of them is a hypothesis to be tested on
launches it has never seen, so training splits strictly by launch time and `compare` reports confidence intervals.

Recorder on a server for days of data: see `deploy/README.md`. Plan and status: `ROADMAP.md`.

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
- Entry and exit each pay the **pump fee** (default 1.238%, measured on live quotes) and a flat **per-tx cost**
  (default 0.003 SOL priority fee/tip). At 0.1 SOL trades the tx costs alone are ~6% of the position, on top of ~2.5% in pump fees (a real quoted round trip at 0.1 SOL loses about 3.1%).
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
- **Checked against the live services (Oct 2026):** PumpPortal's `create` messages parse correctly (25 of 25
  real ones) and the launch state is exactly 30 vSOL / 1.073B tokens; DexScreener lists fresh pump.fun tokens
  before migration; Jupiter routes most of them, with a ~3% quoted round trip that matches the fee model.
- **PumpPortal's trade stream is not free.** `subscribeTokenTrade` and `subscribeAccountTrade` only work with an
  API key funded with at least 0.02 SOL; without one the server replies with an error and sends no trades.
  The recorder needs trades, so it needs either that key or a different trade source (see below).
- **Still unverified** (no reachable Solana RPC from my sandbox): the RPC-backed scanner checks, the pump.fun /
  PumpSwap / Raydium program IDs in `scanner/scan.py`, the PumpPortal `buy`/`sell` message fields, the fee rate.
- A Jupiter "no sell route" on a brand-new token can be indexing lag rather than a honeypot: one fresh token
  showed a buy route but no sell route at any size. The scanner flags it, but treat it as unconfirmed.
- Thresholds in `checks.Thresholds` and `paper.Strategy` are starting points, not tested truths.

## XRPL arbitrage scanner (`xrpl_arb`)

A separate, read-only tool for the XRP Ledger: finds `XRP -> token -> ... -> XRP` cycles across order books and AMM
pools and reports what each would return after fees. No key, no transactions. Built after looking at
lab.bwif.meme, which turned out to be a dashboard for someone else's bot (the strategy is not on the page).
Details, what was verified against mainnet and what was not: [`xrpl_arb/README.md`](xrpl_arb/README.md).

```
python -m xrpl_arb scan --config xrpl_arb/markets.example.json
```
