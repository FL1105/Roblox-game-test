> **PRELIMINARY (Amendment 2).** Covers only the markets already downloaded when it was run: 2025-12, the first 245 markets of 2026-01, and 2026-08-21 to 2026-10-21 (7,628 of 29,366). February to 2026-08-20 is not covered. The "400 days" in the title is the requested range, not the coverage.

# Kalshi KXBTC15M late-window backtest (400 days to 20261021)

Entry: 120 s before close. Order: 10 contracts taker, fee x1. Fills assume the quoted best price; see KNOWN LIMITATIONS in PREREGISTERED.txt.

## Data

- Settled markets in range: 29366
- Usable at entry: 7033
- Dropped or lacking data, no candle at entry: 127
- Dropped or lacking data, settled in range but not downloaded (--cached-only): 21738
- Dropped or lacking data, spread too wide: 468
- Coinbase direction agrees with Kalshi's recorded result: 96.4% of 7033 (far below ~97% means a time-alignment problem, not a market finding)
- Median |Kalshi strike - Coinbase at window start|: 0.7 bps (the exchange-versus-index offset; trend is measured against Coinbase's own start price)
- Close times: 1765467900 to 1792561500 (unix)

## Pre-registered strategies (these get verdicts)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| **trend_open** in-sample | 3667 | 88.5% | 89.9 | -1.32 | [-2.25, -0.40] | 0.997 | -1.47% |
| **trend_open** out-of-sample | 2670 | 87.9% | 89.9 | -2.04 | [-3.09, -0.99] | 1.000 | -2.27% |
| trend_open all | 6337 | 88.3% | 89.9 | -1.63 | [-2.32, -0.93] | 1.000 | -1.81% |
| **trend_mom3** in-sample | 3808 | 57.3% | 58.8 | -1.52 | [-2.42, -0.63] | 1.000 | -2.59% |
| **trend_mom3** out-of-sample | 2689 | 59.5% | 59.5 | -0.03 | [-1.06, +1.00] | 0.524 | -0.05% |
| trend_mom3 all | 6497 | 58.2% | 59.1 | -0.91 | [-1.58, -0.23] | 0.996 | -1.53% |
| **trend_kalshi** in-sample | 3644 | 83.7% | 84.9 | -1.19 | [-2.12, -0.26] | 0.994 | -1.40% |
| **trend_kalshi** out-of-sample | 2658 | 85.6% | 85.7 | -0.15 | [-1.20, +0.90] | 0.611 | -0.18% |
| trend_kalshi all | 6302 | 84.5% | 85.2 | -0.75 | [-1.45, -0.06] | 0.983 | -0.88% |
| **model_edge** in-sample | 1229 | 38.3% | 37.4 | +0.94 | [-1.24, +3.11] | 0.199 | +2.51% |
| **model_edge** out-of-sample | 996 | 25.4% | 28.3 | -2.94 | [-5.20, -0.68] | 0.995 | -10.36% |
| model_edge all | 2225 | 32.5% | 33.3 | -0.80 | [-2.37, +0.78] | 0.840 | -2.39% |
| **favorite** in-sample | 3666 | 88.7% | 90.4 | -1.75 | [-2.67, -0.82] | 1.000 | -1.93% |
| **favorite** out-of-sample | 2670 | 89.2% | 90.7 | -1.52 | [-2.56, -0.47] | 0.998 | -1.67% |
| favorite all | 6336 | 88.9% | 90.6 | -1.65 | [-2.34, -0.96] | 1.000 | -1.82% |

**Verdicts** (rule in PREREGISTERED.txt, alpha = 0.010):

- `trend_open`: no edge shown
- `trend_mom3`: no edge shown
- `trend_kalshi`: no edge shown
- `model_edge`: no edge shown
- `favorite`: no edge shown

**What the pattern feels like** (all trades; a win pays the left column, a loss costs the right one):

| strategy | trades | avg win ¢ | avg loss ¢ | wins to repay 1 loss | break-even win rate | actual win rate | longest losing streak | max drawdown ¢/contract |
|---|---|---|---|---|---|---|---|---|
| trend_open | 6337 | +7.5 | -70.6 | 9.4 | 89.9% | 88.3% | 4 | 12326 |
| trend_mom3 | 6497 | +12.2 | -19.1 | 1.6 | 59.1% | 58.2% | 11 | 7061 |
| trend_kalshi | 6302 | +8.4 | -50.7 | 6.0 | 85.2% | 84.5% | 5 | 6537 |
| model_edge | 2225 | +40.5 | -20.7 | 0.5 | 33.3% | 32.5% | 33 | 4206 |
| favorite | 6336 | +7.5 | -74.6 | 10.0 | 90.6% | 88.9% | 3 | 12064 |

## Controls (no verdict)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| always_yes | 6673 | 47.7% | 49.1 | -1.43 | [-2.09, -0.77] | 1.000 | -2.90% |
| always_no | 6697 | 47.1% | 47.9 | -0.72 | [-1.38, -0.06] | 0.984 | -1.50% |

## Exploratory (no verdicts; cannot rescue a failed strategy)

### Price buckets: win rate against the price paid

| price paid | observations | avg price ¢ | win rate | 95% CI | EV after fee ¢ |
|---|---|---|---|---|---|
| 0-5¢ | 3903 | 1.4 | 1.3% | [1.0%, 1.7%] | -0.24 |
| 5-10¢ | 951 | 7.0 | 6.6% | [5.2%, 8.4%] | -0.87 |
| 10-15¢ | 467 | 12.0 | 13.5% | [10.7%, 16.9%] | +0.70 |
| 15-20¢ | 337 | 16.7 | 18.4% | [14.6%, 22.9%] | +0.65 |
| 20-25¢ | 252 | 21.9 | 23.0% | [18.2%, 28.6%] | -0.08 |
| 25-30¢ | 231 | 27.0 | 25.1% | [20.0%, 31.1%] | -3.33 |
| 30-35¢ | 229 | 31.8 | 35.8% | [29.9%, 42.2%] | +2.38 |
| 35-40¢ | 215 | 36.8 | 34.0% | [28.0%, 40.5%] | -4.59 |
| 40-45¢ | 213 | 42.3 | 42.3% | [35.8%, 49.0%] | -1.83 |
| 45-50¢ | 192 | 47.3 | 43.2% | [36.4%, 50.3%] | -5.85 |
| 50-55¢ | 197 | 52.1 | 50.8% | [43.8%, 57.7%] | -3.12 |
| 55-60¢ | 203 | 57.0 | 59.6% | [52.7%, 66.1%] | +0.77 |
| 60-65¢ | 183 | 62.1 | 61.2% | [54.0%, 68.0%] | -2.58 |
| 65-70¢ | 223 | 66.9 | 65.0% | [58.6%, 71.0%] | -3.46 |
| 70-75¢ | 241 | 71.9 | 68.0% | [61.9%, 73.6%] | -5.35 |
| 75-80¢ | 239 | 77.0 | 73.6% | [67.7%, 78.8%] | -4.68 |
| 80-85¢ | 274 | 82.1 | 81.0% | [76.0%, 85.2%] | -2.17 |
| 85-90¢ | 389 | 87.1 | 82.0% | [77.9%, 85.5%] | -5.87 |
| 90-95¢ | 753 | 92.5 | 90.0% | [87.7%, 92.0%] | -2.94 |
| 95-100¢ | 3678 | 98.6 | 98.5% | [98.0%, 98.8%] | -0.20 |

Each market contributes two observations (its YES and NO ask), which are mirror images, so the counts are doubled and the rows are not independent.

### model_edge threshold

| min edge | trades | win rate | mean P&L ¢ | p(mean>0) | OOS trades | OOS mean ¢ |
|---|---|---|---|---|---|---|
| 0¢ | 5318 | 53.6% | -0.63 | 0.946 | 2211 | -2.14 |
| 1¢ | 3345 | 40.1% | -0.76 | 0.901 | 1399 | -2.61 |
| 2¢ | 2645 | 35.3% | -0.84 | 0.879 | 1143 | -2.94 |
| 3¢ | 2225 | 32.5% | -0.80 | 0.840 | 996 | -2.94 |
| 5¢ | 1619 | 30.3% | -0.50 | 0.696 | 754 | -3.03 |
| 8¢ | 1076 | 29.0% | -0.87 | 0.758 | 531 | -3.98 |

### Entry time (mean P&L ¢ per contract, all markets)

| entry | trend_open | trend_mom3 | trend_kalshi | model_edge | favorite |
|---|---|---|---|---|---|
| 300 s left (10:00) | -0.93 (n=6969) | -1.22 (n=6977) | -0.60 (n=6889) | -1.09 (n=2766) | -1.16 (n=6968) |
| 240 s left (11:00) | -1.01 (n=6934) | -0.41 (n=6945) | -0.63 (n=6841) | -0.83 (n=2728) | -1.07 (n=6934) |
| 180 s left (12:00) | -1.69 (n=6742) | -0.80 (n=6790) | -0.86 (n=6677) | -1.46 (n=2534) | -1.68 (n=6742) |
| 120 s left (13:00) | -1.63 (n=6337) | -0.91 (n=6497) | -0.75 (n=6302) | -0.80 (n=2225) | -1.65 (n=6336) |

### Probability forecast quality

7033 markets; lower is better; always guessing 50% scores 0.2500.

| forecast | Brier score |
|---|---|
| model (gap / volatility) | 0.0773 |
| market mid-price | 0.0722 |
| 50/50 blend | 0.0734 |
