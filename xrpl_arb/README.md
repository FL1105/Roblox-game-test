# XRPL arbitrage scanner

Finds `XRP -> token -> ... -> XRP` cycles across the XRP Ledger's order books and AMM pools and says what each
would return after fees. **Read-only: it holds no key and builds no transaction.** Standard library only.

```
python -m unittest tests.test_xrpl_arb                                      # 53 tests, no network
python -m xrpl_arb scan --config xrpl_arb/markets.example.json              # one scan
python -m xrpl_arb scan --config pairs.json --loop 10 --out data/xrpl.jsonl # every 10 s, recorded
```

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

## Limits you should know about

- **It polls.** A 98-pair sweep takes about 45 s (about 12 ledgers). A bot that sees each ledger as it closes (a
  websocket `subscribe` and incremental updates) will win any opportunity this finds. Fine for measuring whether an
  edge exists, not for capturing one.
- **Fee asymmetry.** In the DROP and PHNIX pools one operator holds the live auction slot (with four authorized
  accounts) and pays a tenth of the trading fee; anyone else starts 0.35% behind on those pools.
- **Dust.** The site's own numbers are hundredths of an XRP per cycle; one failed batch costs the fee of dozens of wins.
- One snapshot shows state, not a fill. Do not read `profit` as money earned.

## Not built: execution

There is deliberately no code that signs or submits. The honest next steps, in order: (1) record opportunities for a
few days and count how long each one lives (`--out`), (2) decide dry-run only versus a funded wallet with a hard loss
cap, (3) build the submit path as one atomic `Batch` (all-or-nothing) and test it on the XRPL testnet first. None of
that should start before step 1 shows opportunities that persist longer than your latency.
