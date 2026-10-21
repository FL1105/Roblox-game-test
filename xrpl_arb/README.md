# xrpl_arb: XRP Ledger arbitrage bot with a live dashboard

Scans the XRP Ledger's order books and AMM pools every ledger, finds cycles that end with more than they started with, and (optionally) trades them. A browser dashboard shows all of it live.

**It starts in paper mode. Paper mode holds no wallet or key and sends nothing: it only reads public ledger data.** Live trading is a separate, explicitly gated mode (see below) that has not been run with real funds.

```
python -m xrpl_arb                    # real mainnet data, paper trading, dashboard at http://127.0.0.1:8780
python -m xrpl_arb --demo             # synthetic markets, for trying the dashboard offline (nothing real)
python -m xrpl_arb.markets            # one-shot look at every pool and book, no dashboard
python -m xrpl_arb --ledgers 20 --no-browser    # headless: scan 20 ledgers, print a summary
python -m unittest tests.test_xrpl_arb          # 75 tests, no network needed
```

Python 3.10+, standard library only. Live trading additionally needs `pip install xrpl-py`.

## What it looks at

Every ledger (about every 4 s) it reads, from one validated ledger so the pieces agree: each watched pair's AMM pool (`amm_info`) and both directions of its order book (`book_offers`), plus each issuer's transfer fee. Default pairs: XRP against RLUSD, USD (Bitstamp), USD (GateHub), EUR (GateHub), SOLO and CORE, plus SOLO/CORE and RLUSD/USD (Bitstamp). Change them with `--markets XRP/RLUSD,XRP/SOLO,...` (names are in `config.py`: `KNOWN`).

A **cycle** is a closed loop through those markets that starts and ends in the base asset (default XRP, and profit is counted in it): out and back over one market (`XRP → SOLO → XRP`: buy from the cheaper of pool and book, sell to the dearer), or a triangle (`XRP → SOLO → CORE → XRP`). For each cycle it finds the trade size, between `--size-min` and `--size-max`, that maximises net profit.

## How a leg is priced

A payment through a pair uses the order book and the AMM together, taking whichever is cheaper at the margin. `liquidity.py` reproduces that: the pool is used until its post-trade price equals the next offer's price, then the offer is taken, and so on. The pool keeps its fee, and the payer pays the issuer's transfer fee on the issued currency it sends **and** the one it receives.

This was checked against the ledger itself, not just reasoned about. The server's `simulate` call runs an unsigned payment against the live open ledger without applying it, so the model could be compared with the real engine on real pools:

| Where | Result |
|---|---|
| Book only, both directions, many sizes | identical to the engine (0.00 bps) |
| Pool-dominated (SOLO, USD Bitstamp, USD GateHub), 69-100% pool | within 0.0 to 1.8 bps; exact for the pool-only cases |
| Mixed book and pool | within about 2 bps |
| Very large sizes (100k+ units, far past the default cap) | optimistic by 40 to 190 bps: do not trade that big on this model |

Two things this turned up: the output-side transfer fee (the first version only charged the input side, which made GateHub assets look 20 bps better than they are), and that the model must see the whole book it might trade into: with only 40 offers fetched it understated what 20k units could buy. The bot never sizes a trade past the depth it has fetched (`book_depth`, default 40).

## Why every leg is its own transaction

The obvious design is one atomic transaction that goes round the cycle and fails if it does not end up ahead. The ledger does not allow it, checked with `simulate`:

* An XRP to XRP payment with `SendMax` or `Paths` is malformed (`temBAD_SEND_XRP_MAX`).
* A cycle in an issued currency (RLUSD to XRP to RLUSD) is rejected as a path loop (`temBAD_PATH_LOOP`) whichever way the issuer fields are set.
* The `Batch` amendment, which would make several transactions all-or-nothing, was **not enabled on mainnet** when this was written (it is known to the server software, so it may be later: look at the Amendments object).

So a cycle is a sequence of payments, one per leg, each sent after the previous one validates. Between legs the wallet holds the intermediate asset, which is real **legging risk**. `plan.py` is the policy that contains it, shared by paper and live:

1. **First leg** must fill within `slip_bps` (10) of its quote, or the cycle is dropped before any money has moved.
2. **Middle legs** must fill within `max_loss_bps` (100) of their quote.
3. **Final leg** must return the stake plus the required profit. If the market moved, one more attempt (the *unwind*) accepts anything within `max_loss_bps` of the stake.
4. If even the unwind fails the wallet is holding the intermediate asset: the trade is **STUCK**, the bot halts, and a person has to look.

Each leg is a partial payment from the wallet to itself with `DeliverMin` as the price guard. It is first run through the server's `simulate` and is only sent if that succeeds and delivers at least the guard, so a leg that would fail costs nothing.

## Paper mode is not optimistic by design

A paper trade opened at ledger N sends its first leg at N+1, its second at N+2, and so on, each priced from that later ledger's real books and pools. That includes the cost of being late and of the market moving between legs. The dashboard shows two P&L lines: **quoted at detection** (what you would have if every cycle filled at the price you saw) and **realized after legs**. The gap between them is the point.

It is still not a promise: it assumes you are the only one acting on a given price in a given ledger. Other bots racing for the same gap will beat you to some of them.

## The dashboard

`http://127.0.0.1:8780`, updated live over Server-Sent Events (falls back to polling).

* **Banner**: PAPER (real data, nothing sent), DEMO (synthetic), or LIVE TRADING.
* **Best cycle right now**: the headline number, with what a trade needs.
* **Best cycle edge, every ledger**: net edge in basis points per ledger against the trade threshold.
* **Cumulative profit**: quoted vs realized.
* **Cycles**: every route, its optimal size, net profit, edge, and whether each leg would go through the pool or the book.
* **Markets**: pool size and fee, book bid/ask, spread, and the **venue gap**: the best price difference between pool and book after the pool fee. Positive means a tiny trade could cross them.
* **Trades**, **Events**, a Start/Stop switch (Stop keeps scanning but opens nothing), and **Settings** (thresholds and sizes, validated and applied live). Each chart has a Table button.

It serves on `127.0.0.1` only, refuses foreign `Host` headers and non-JSON POSTs, renders all chain data as text (never HTML), and never receives or displays a key.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `min_profit_bps` / `min_profit_base` | 10 / 0.05 | a cycle must clear **both**, net of fees |
| `size_min` / `size_max` | 10 / 500 | trade size range, base units |
| `slip_bps` | 10 | how far the first leg may fill below its quote |
| `max_loss_bps` | 100 | how far an unwind may sell below the stake |
| `cooldown_ledgers` | 3 | after trading a route, ledgers before trading it again |

## Live trading

**Read this properly before using it. It has not been run with real funds**, so start tiny. What has been verified: the transaction builder against the real engine through `simulate` (guards below the quote succeed, impossible guards are refused with `tecPATH_PARTIAL`), local signing with a throwaway wallet, and the whole state machine against a scripted fake server.

```
pip install xrpl-py
export XRPL_SEED=sXXXX...        # a dedicated wallet holding only what you are willing to lose
python -m xrpl_arb --trade-live --max-trade 25
```

* The seed comes **only** from the `XRPL_SEED` environment variable (never an argument, never logged, never in the dashboard). Use a new wallet, not your main one.
* `--max-trade N` is required: the most base units one cycle may put at risk. Sizes are also capped by what the wallet can spend after its reserve.
* The wallet needs XRP for its reserve and fees, and a **trust line** for every issued currency a cycle touches (routes without one are shown as NOT TRADABLE).
* On start it runs the real transaction builder through `simulate` once; if the server rejects it, live mode refuses to start. Then there is a 10 second countdown.
* It halts itself on a STUCK trade, three losing trades in a row, or `--max-session-loss N`. Halting stops new trades; a trade already in flight finishes its legs.
* A halted bot stays halted until someone presses Resume on the dashboard.

What can still go wrong: the market moves between legs and an unwind sells at a loss (bounded by `max_loss_bps`, but real); a leg validates and its successor does not; a public server drops mid-trade; fees spike. Public servers add latency, and a serious competitor runs its own node.

## Files

| File | What it does |
|---|---|
| `assets.py`, `liquidity.py`, `routes.py` | amounts, the pool+book model, cycle search and sizing |
| `rpc.py`, `feed.py` | JSON-RPC client with failover; live and synthetic data feeds |
| `plan.py`, `executor.py`, `paper.py`, `live.py` | the trade policy, shared bookkeeping, paper and live execution |
| `bot.py`, `state.py`, `config.py` | the per-ledger loop, dashboard state, validated settings |
| `server.py`, `static/index.html` | local server and the dashboard (one file, no external requests) |
| `markets.py` | `python -m xrpl_arb.markets` |
