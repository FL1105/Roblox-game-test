# poly_mm: Polymarket 5-minute BTC market-making research (read-only)

Public APIs only: no wallet, no key, no order code.

```
python -m poly_mm fetch --hours 72       # cache the taker trade tape of every resolved 5-minute BTC Up/Down market
python -m poly_mm backtest --hours 72    # paper market maker over those tapes; picks on the first half, reports the second
python -m unittest tests.test_poly_mm
```

## Method
A taker BUY of one outcome at p is the same as a taker SELL of the other at 1 - p (the exchange mints a pair), so every tape row is pressure on one outcome's bids. A resting bid of ours at b is filled for as many shares as were sold below b (we would have been best bid), and for `queue` of the shares sold at exactly b. We fill at our own price, pay no fee and take no rebate. Markets settle on their real outcome. Fair value is the last trade, a static 0.50, or a price implied by Coinbase one-minute BTC candles.

## Result (865 markets, 1.27M trades, 72 hours to 2026-10-21)
Every variant loses on the held-out half. The tweet's static 0.48/0.48: -$5.74 a market, -14% of dollars filled (barely moves with the queue assumption). Following the last price: -5% to -7% of volume. Quoting off BTC: -14% to -20%, worse, so a one-minute BTC price is stale against the people trading these markets. Skewing quotes against inventory shrinks the leftover position but turns the paired profit negative.

Mechanism: when BTC trends, the losing side is sold into your standing bid all window while the winning side never fills, so you end up holding the loser.

## What this does not show
The wallet that prompted this (`norm1e69`) earns about 0.8% of volume per Polymarket's leaderboard. This simulator cannot reproduce that, and queue assumptions do not close the gap. Candidates it does not model: faster or better price information, cancelling before the price moves, maker rebates (20% of taker fees) and liquidity rewards, and order-book depth, which Polymarket does not publish historically. It is also not proof the wallet's figures are real.
