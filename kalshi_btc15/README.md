# Kalshi 15-minute BTC: trade the last 2.5 to 2 minutes

Backtests "if BTC is trending up take the over (YES), if down take the under (NO)" on Kalshi's `KXBTC15M` series,
alongside a gap-versus-volatility model and a price-bucket (favorite-longshot) check. **Read-only: public market
data, no API key, no orders.** Standard library only, Python 3.10+.

```
python -m kalshi_btc15 probe                    # print the raw API responses this code relies on (run this first)
python -m kalshi_btc15 backtest --days 400      # download (cached) and write kalshi_btc15/reports/report_<date>.md
python -m kalshi_btc15 backtest --no-spot       # Kalshi only, no Coinbase (only the Kalshi-price strategies run)
python -m unittest tests.test_kalshi_btc15      # 28 tests, no network needed
```

Needs outbound HTTPS to `api.elections.kalshi.com` (markets, candles) and `api.exchange.coinbase.com` (BTC-USD
minutes). Downloads are cached under `kalshi_btc15/cache/` (git-ignored), so a rerun only fetches what is new.

**Read `PREREGISTERED.txt` (and `PREREGISTERED_amendment1.txt`) first.** It fixes the five strategies, the fill and fee model, the in-sample / out-of-sample
split and the decision rule before any result exists. The report applies that rule mechanically; the exploratory
sections (price buckets, threshold sweep, entry-time sweep, Brier scores) never produce a verdict.

| file | what it does |
|---|---|
| `api.py` | Kalshi client, defensive parsing (cents or dollar strings), disk cache, live and `/historical` endpoints |
| `spot.py` | Coinbase BTC-USD minute candles and trailing volatility |
| `data.py` | one `Sample` per market as seen at the entry moment (nothing after it except the result) |
| `strategies.py` | the five strategies, two controls, the fee and the fill |
| `stats.py`, `report.py` | summaries, confidence intervals, drawdown, losing streaks, the Markdown report |

Candles are 1-minute, so "12:30 into the window" cannot be observed; the entry is the quote at the 13:00 mark
(2:00 left), with 3:00, 4:00 and 5:00 left reported as a sensitivity check. The synthetic worlds in the tests only prove
the harness finds an edge when one is planted and none when the market is fair; they say nothing about the real market.
