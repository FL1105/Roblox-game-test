# pmarb: prediction-market arbitrage scanner (Kalshi + Polymarket)

Read-only research tooling. Public endpoints only: **no API keys, no wallets, no orders.** Standard library only.

```
python -m pmarb scan                       # one pass over every open market on both venues, all detectors, fees included
python -m pmarb watch --minutes 40         # poll exactly-matched games and live BTC 15-minute windows every 20 s, log every gap
python -m pmarb btc3 --minutes 45          # the same BTC 15-minute window on Kalshi, Polymarket and Limitless, fetched concurrently, with sizes
python -m pmarb band --windows W --history H   # score the pre-registered 'sell the band' hedge (see PREREGISTERED_band.txt)
python -m pmarb bankroll --log <watch log> # fee rounding by order size; what a bankroll could capture
python -m pmarb ui                         # PAPER bot + live P&L dashboard at http://127.0.0.1:8766/ (Windows: double-click start_pmarb.bat)
python -m pmarb bot --minutes 110          # the same paper bot without the dashboard, printing to the console
python -m unittest tests.test_pmarb        # offline tests, hand-checked arithmetic
```

## What it checks

| detector | the hedge | risk-free when |
|---|---|---|
| `sell_all` | buy NO on every outcome of a mutually exclusive event; pays at least N - 1 | the outcomes really are mutually exclusive |
| `implication` | buy YES on "A" and NO on "B" where B implies A (threshold ladders); pays at least 1 | same subject, same close, same rules text apart from the number |
| `cross_game` | the same team winning, YES on one venue and NO on the other | both venues settle the game identically |
| `buy_all` | buy YES on every outcome | **never claimed**: needs the listed outcomes to be exhaustive, which the APIs do not say |

Every profit figure is **after the taker fee on every leg** and sized to the quantity the top of the book supports.
Kalshi: `ceil(0.07 * multiplier * C * p * (1 - p))` to the cent, multiplier per series from `/series`. Polymarket:
`C * rate * p * (1 - p)` with the rate and exponent from each market's own `feeSchedule` (crypto 0.07, politics 0.04,
geopolitics and the sports games checked 0). Makers pay nothing on Polymarket and on Kalshi's plain `quadratic` series, **but 160 Kalshi
series are `quadratic_with_maker_fees` (makers pay too); check a series' `fee_type` before assuming a free maker leg.

## Pitfalls this code already hit (each has a regression test)

* Comparing thresholds across different players or teams in one Kalshi event invented $728k of "risk-free" profit.
* Kalshi tags exact-count bins ("exactly 3 launches") as `strike_type: less` with floor == cap: not a ladder.
* Fuzzy title matching across venues produces "+97% arbitrage" from complements, different rounds and reversed scorelines.
  Only exact keys (date + both team codes) are used for automatic matching; fuzzy candidates need a human to read both rules.
* Polymarket offset paging stops at 2,000 events (HTTP 422); use `/events/keyset` with `after_cursor`.
* Kalshi 15-minute BTC and Polymarket's BTC up/down settle on different sources (BRTI 60 s average vs Chainlink TWAP) with
  different start references. A gap there is basis risk, not arbitrage: the hedge can pay $0 (measured: the two
  disagreed on 1.33% of 1,200 windows).


## The live dashboard (`python -m pmarb ui`)

Runs the paper bot against the live Kalshi and Polymarket books and shows its P&L as it happens: today and all-time realised, open
positions, an equity curve, per-window results with the payoff counts (any 0 or 2 is a venue disagreement), the limits in use, and a
plain-English line for why the bot is or is not trading. **It places no orders and has no key fields.** It must run on your own
computer, because Kalshi rejects browser requests (any `Origin` header gets a 403), so the page talks to a small local Python
server instead of the venues. The server binds to 127.0.0.1, refuses foreign Host headers and takes only JSON POSTs.

* The ledger (`pmarb_bot_ledger.jsonl`) is appended to and restored on start, so open positions and history survive a restart.
* "Pause new trades" stops new paper trades; open positions still settle. The daily loss stop is per UTC day.
* To compare with real trading, type your actual P&L for each window in the table; the last column is actual minus paper.
* Options: `--port`, `--interval`, `--ledger`, `--max-sets`, `--max-window-cost`, `--max-open-cost`, `--loss-stop`, `--no-browser`.
