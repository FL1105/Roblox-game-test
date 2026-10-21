# XRPL arbitrage scanner

Finds `XRP -> token -> ... -> XRP` cycles across the XRP Ledger's order books and AMM pools and says what each
would return after fees. **Read-only: it holds no key and builds no transaction.** Standard library only.

```
python -m unittest tests.test_xrpl_arb tests.test_xrpl_ui                    # 87 tests, no network
python -m xrpl_arb scan --config xrpl_arb/markets.example.json              # one scan
python -m xrpl_arb scan --config pairs.json --loop 10 --out data/xrpl.jsonl # poll every 10 s, recorded
python -m xrpl_arb watch --config pairs.json --out data/xrpl_watch.jsonl    # react to every ledger close
python -m xrpl_arb ui --config pairs.json                                   # the same, with a dashboard
python -m xrpl_arb ui --config pairs.json --replay data/xrpl_watch.jsonl    # look at a recording
```

`watch` and `ui` need `pip install websockets` (the only dependency, same as the rest of the repo's live tools).

## What lab.bwif.meme is (and isn't)

It is a **dashboard** for two bot wallets run by someone else ("bwif Bots - Real-time arbitrage cycle
visualization"), tied to a memecoin project. The strategy code is server-side and not on the page, so there is no
spec to copy; what the page's public JSON feeds show about the bot (self-reported, not independently checked):

- **Shape of a trade:** an atomic `Batch` transaction (all-or-nothing) of 3 to 5 swaps that starts and ends in XRP,
  for example `XRP > BEAR > $TRUMP > XRP`. Mainnet has the amendment (`BatchV1_1`, `fixBatchV1_2` enabled; checked).
- **Universe:** about 40 microcap tokens that trade against XRP, plus some token/token pools.
- **Size of the edge:** 60 recent cycles had a median profit of 0.0046 XRP (mean 0.021, max 0.19), batch fees of 40 to
  111 drops. The page claims 234,335 cycles, 86.9% "winning", 2,645 XRP net all-time, about 195 XRP/day on about 15k XRP
  of daily input. Failed attempts cost the fee and are included in its PnL.
- **Copy / revenue features** and a token swap iframe: not part of the arbitrage and not reproduced here.

## How the scanner works

1. **Snapshot** (`snapshot.py`): reads both order books and the AMM pool of every pair in the config, all pinned to
   one validated ledger and fetched in parallel; falls back (and says so) if a node lacks that ledger.
2. **Quote** (`liquidity.py`): a swap is split between the book and the pool the way the ledger does it: the pool is
   swapped just far enough that its price meets the next offer, then the offer is taken, and so on. Issuer transfer
   fees are charged on what we spend. The pool's trading fee is the full one unless `--account` is a member of the
   pool's live auction slot (then the discounted fee).
3. **Search** (`cycles.py`): all simple XRP cycles of 2 to `--max-legs` legs. Each leg's output is concave in its
   input, so a cycle can only pay if the product of its legs' spot rates exceeds 1 (a cheap exact filter), and the
   best input size is found by golden-section search. XRP is rounded down to whole drops.
4. **Report**: profit is net of `--tx-cost`; a cycle must clear `--min-profit` (XRP) and `--min-profit-pct`.
   A scan that finds nothing says how close it came: the best cycle at its best size ("best below your
   thresholds"), or, if nothing pays at any size, the nearest cycle's return. With `--out`, every scan is a
   JSONL row (ledger, counts, near miss) and every opportunity its own row with the legs.

Config is JSON: `{"assets": ["USD+rIssuer", "DROP+rIssuer", ...]}` pairs every asset with XRP and with each other
(cost grows with the square of the list), or `{"pairs": [["XRP", "DROP+r..."], ["DROP+r...", "PLX+r..."]]}` for an
explicit list. Names that aren't exactly 3 characters (`DROP`, `bwif`, `X`) are encoded to the ledger's 40-digit hex
for you. Every issuer address is checksum-validated before any request.

## What was checked against mainnet, and what wasn't

Method: quote spending N XRP on a token with the model, then ask `ripple_path_find` (the ledger's own payment
engine, pinned to the same ledger as the snapshot) what delivering that amount costs. 96 comparisons: DROP, PLX, ATM
and FUZZY at 2, 10 and 50 XRP over 8 different ledgers, plus single-ledger probes up to 100 XRP.

- Response shapes (`book_offers`, `amm_info`, `account_info`) parse correctly on live data. Two bugs only real data
  could show (a `null` auction-slot list, a snapshot that spanned several ledgers) were found and fixed this way.
- **Model too generous** (it would report an edge the ledger won't give): usually under 0.02%; worst seen +0.24%
  (PLX, 50 XRP, one ledger), +0.057% at 100 XRP in a pure-pool case. The default `--min-profit-pct 0.25` sits just
  above that worst case. It is a margin from 96 samples, not a guarantee.
- **Model too pessimistic**: the engine was up to 0.94% cheaper in some ledgers. Cause found: it routes through
  another token (FUZZY via gei, POLICE, 3RDEYE or WIND). That is a better *buy* route, not an arbitrage on its own,
  since the return leg through the direct pool pays another 0.63% fee.
- A slot holder's quotes were cheaper by exactly the trading fee minus the discounted fee (0.393% - 0.039%).
- 72% of the 96 comparisons were within 0.05% either way.

Not checked:

- Whole closed cycles (`path_find` does not search XRP -> ... -> XRP loops), so multi-leg cycles are validated leg by
  leg, not end to end.
- Trades above 100 XRP (the test wallet could not fund path-finding above that).
- Whether pools pay the issuer's transfer fee. The model charges it (conservative).

## What a live run found (Oct 2026, one public server, 98 pairs, 4-leg limit, 25 XRP cap, 0.001 XRP / 0.25% thresholds)

Two runs, 29 scans over about 230 ledgers (roughly 13 minutes). Treat it as a small sample, not a measurement of the market.

- 27 scans found nothing that passes the thresholds. In the second run (15 scans; the first run did not record this)
  every scan had a cycle that pays before the 0.0001 XRP fee, but in the 13 that found nothing the best gained only
  0.00003 to 0.00015 XRP at its best size (0.007 to 0.4 XRP in): fee-sized dust.
- 2 scans (both in the second run) each showed a burst of overlapping cycles that passed: 5 cycles at ledger
  107798500 (0.0011 to 0.0015 XRP, 2.4% to 3.1% on about 0.05 XRP) and 10 at ledger 107798590 (up to 0.0066 XRP, 2.6%
  on 0.26 XRP), through XRPX, BITx, PLX and GRIM. The cycles in a burst share legs, so each burst is one mispricing, not
  15 separate profits. Both were gone at the next scan, 7 to 8 ledgers later.
- Cross-check of the first burst against the engine, pinned to that ledger: the engine also found routes to BITx about 2%
  cheaper than the direct pool, so the mispricing looks real, but its cheapest route cost about 0.7% more than the model's.
  The model is optimistic here, on tiny pools (one held 0.78 XRPX at a 0% fee).
- That is the same size of prize the lab.bwif.meme feed reports (median 0.0046 XRP per cycle), which is why polling at
  this speed is the wrong tool for capturing it.

## Watching ledger by ledger (`watch`, `ui`)

`scan` polls: a 98-pair sweep takes about 45 s, which is 12 ledgers. `watch` subscribes to the `ledger` and
`transactions` streams over a websocket instead. It maps each validated transaction's metadata to the configured
pairs it touched (an `Offer` node names its two assets; a swap moves the AMM account's `AccountRoot` or
`RippleState`), re-reads only those pairs pinned to the ledger that just closed, re-evaluates every cycle, and follows
each opportunity until it disappears, so its lifetime is measured in ledgers. A dropped connection reconnects, and
every pair is re-read on connect and every `--full-every` ledgers as a safety net.

- **Which pairs changed is checked, not assumed.** Over four consecutive mainnet ledgers I compared every pool and book at
  ledger N-1 and N: all 9 pairs that really changed were flagged, none missed, none flagged for nothing. Small sample.
- Transactions of older ledgers can arrive after a newer ledger's close message, and `Batch` parents carry only their
  own three metadata nodes (the inner swaps arrive as separate messages). So the stream only decides which pairs to
  re-read; the state itself is always read from the ledger.
- **Measured** (one run, 100 ledgers, 98 pairs, 8 minutes): median 3 pairs re-read per ledger (max 27); a ledger was
  evaluated a median of 1.42 s after its close message arrived (90% within 2.1 s, worst 6.0 s), against 45 s polling.
  In that run 3 ledgers had an opportunity that cleared the thresholds, all in one burst at ledger 107799078: 5
  overlapping cycles through ARMY, POLICE, FUZZY and gei, up to +0.032 XRP (0.59%) on 5.4 XRP. They stayed open 1, 1, 1,
  3 and 3 ledgers. A sample of one burst says little about how often this happens.

`ui` runs the same watcher with a dashboard on `http://127.0.0.1:8765` (read-only, loopback only): the best cycle in each
ledger on a log scale against the fee and the bar to clear, live and ended opportunities with their legs and lifetimes,
how fast each ledger was handled, and a table of every market. The page makes no outside requests and shows chain data
(token names are chosen by whoever issues them) only as text; a browser test feeds it a hostile name to check.

## Limits you should know about

- **Detecting is not capturing.** `watch` sees a ledger about 1.4 s after it closes, but this package cannot submit, and
  a competitor who submits into the next ledger needs the same window. The opportunities seen lasted 1 to 3 ledgers.
  `scan` is slower still (45 s) and only suits measuring whether an edge exists.
- **Fee asymmetry.** In the DROP and PHNIX pools one operator holds the live auction slot (with four authorized
  accounts) and pays a tenth of the trading fee; anyone else starts 0.35% behind on those pools.
- **Dust.** The site's own numbers are hundredths of an XRP per cycle; one failed batch costs the fee of dozens of wins.
- One snapshot shows state, not a fill. Do not read `profit` as money earned.

## Not built: execution

There is deliberately no code that signs or submits. The honest next steps, in order: (1) record `watch` for days and
count how many opportunities appear and how long each lives, which tells you whether a 1.4 s reaction is ever fast enough,
(2) decide dry-run only versus a funded wallet with a hard loss cap, (3) build the submit path as one atomic `Batch`
(all-or-nothing) and test it on the XRPL testnet first. None of that should start before step 1 shows opportunities that
persist longer than your latency. The XRPL testnet and its faucet were not reachable from the sandbox this was built in.
