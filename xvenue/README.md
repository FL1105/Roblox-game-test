# xvenue: Polymarket vs Kalshi hourly BTC hedge scanner (read-only)

Public market data only: no account, no key, no order code.

```
python -m xvenue scan --minutes 30          # watch the current hour on both venues, print and log every snapshot
python -m unittest tests.test_xvenue
```

Inspired by `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot`, which flags a hedge when two top-of-book asks sum to under $1.

## What it adds to that idea
* Real order-book depth on both venues (it finds the share count that maximises profit, not 1 share at the top).
* Both venues' fees. Polymarket's taker fee is `shares x 0.07 x p x (1-p)` (published; makers pay nothing). Kalshi's is a "quadratic" schedule; the 0.07 coefficient and round-up-to-the-cent are assumed, not read from the API, so check them before trusting small margins.
* The right strike (below).
* Two same-venue checks that need no cross-source assumption: a Kalshi ladder pair (`Yes(low strike) + No(high strike)` always pays at least $1) and Polymarket `Up + Down` (always pays exactly $1).

## The strike problem the reference repo has
Polymarket's hourly market resolves on **Binance BTC/USDT** (close >= open of the hourly candle); Kalshi's ladder resolves on **CF Benchmarks' BRTI**, a USD index. Measured over 73 published hours, Polymarket's strike (its "price to beat") was **$96.9 above Coinbase's BTC-USD price at the same moment (sd $14, never below +$70)**. Comparing Polymarket's strike with Kalshi's strikes directly, as that repo does, therefore picks the wrong hedge for any Kalshi strike in roughly a $100 band, and a wrong hedge can lose on both legs. This scanner translates the strike into USD terms (Coinbase open at the hour start) and skips Kalshi strikes within `--margin` dollars (default 60) of it.

Polymarket also publishes the strike only after the hour ends, so it cannot be read live; the Coinbase open is the estimate.

## Why a cross-venue hedge rarely costs under $1
Up + No at Kalshi strike K >= Kp pays $2 when the price ends between the strikes, so the market charges for that band: fair cost is about `1 + (gap) x (density near the strike)`. With hourly BTC moving about 0.31% (sd, $259), a $100 gap is worth roughly 10 to 15 cents. A hedge only costs under $1 if the venues are mispriced by more than that, plus both fees and both spreads. Observed live: $1.14 to $1.24 all in.
