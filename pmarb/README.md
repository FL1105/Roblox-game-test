# pmarb: prediction-market arbitrage scanner (Kalshi + Polymarket)

Read-only research tooling. Public endpoints only: **no API keys, no wallets, no orders.** Standard library only.

```
python -m pmarb scan                       # one pass over every open market on both venues, all detectors, fees included
python -m pmarb watch --minutes 40         # poll exactly-matched games and live BTC 15-minute windows every 20 s, log every gap
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
geopolitics and the sports games checked 0). Makers pay nothing on either venue.

## Pitfalls this code already hit (each has a regression test)

* Comparing thresholds across different players or teams in one Kalshi event invented $728k of "risk-free" profit.
* Kalshi tags exact-count bins ("exactly 3 launches") as `strike_type: less` with floor == cap: not a ladder.
* Fuzzy title matching across venues produces "+97% arbitrage" from complements, different rounds and reversed scorelines.
  Only exact keys (date + both team codes) are used for automatic matching; fuzzy candidates need a human to read both rules.
* Polymarket offset paging stops at 2,000 events (HTTP 422); use `/events/keyset` with `after_cursor`.
* Kalshi 15-minute BTC and Polymarket's BTC up/down settle on different sources (BRTI 60 s average vs Chainlink TWAP) with
  different start references. A gap there is basis risk, not arbitrage: the hedge can pay $0 (measured: the two
  disagreed on 1.33% of 1,200 windows).
