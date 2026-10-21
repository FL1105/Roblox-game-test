> **FINAL v1 verdict** (Amendment 2 / PREREGISTERED_v2.txt): one run on all DEV markets plus the sampled holdout (12,427 usable markets). The 60/40 time split puts everything up to about 2026-08-20 in-sample and 2026-08-21 to 2026-10-21 out-of-sample. The "400 days" in the title is the requested range.

# Kalshi KXBTC15M late-window backtest (400 days to 20261021)

Entry: 120 s before close. Order: 10 contracts taker, fee x1. Fills assume the quoted best price; see KNOWN LIMITATIONS in PREREGISTERED.txt.

## Data

- Settled markets in range: 29366
- Usable at entry: 12427
- Dropped or lacking data, no BTC data (kept for Kalshi-only strategies): 11
- Dropped or lacking data, no candle at entry: 144
- Dropped or lacking data, settled in range but not downloaded (--cached-only): 16315
- Dropped or lacking data, spread too wide: 480
- Coinbase direction agrees with Kalshi's recorded result: 96.2% of 12419 (far below ~97% means a time-alignment problem, not a market finding)
- Median |Kalshi strike - Coinbase at window start|: 0.8 bps (the exchange-versus-index offset; trend is measured against Coinbase's own start price)
- Close times: 1765467900 to 1792561500 (unix)

## Pre-registered strategies (these get verdicts)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| **trend_open** in-sample | 6137 | 90.3% | 90.2 | +0.05 | [-0.59, +0.70] | 0.436 | +0.06% |
| **trend_open** out-of-sample | 4764 | 87.5% | 89.8 | -2.31 | [-3.12, -1.49] | 1.000 | -2.57% |
| trend_open all | 10901 | 89.1% | 90.0 | -0.98 | [-1.49, -0.47] | 1.000 | -1.09% |
| **trend_mom3** in-sample | 6475 | 56.2% | 57.6 | -1.40 | [-2.02, -0.79] | 1.000 | -2.44% |
| **trend_mom3** out-of-sample | 4787 | 59.7% | 60.3 | -0.62 | [-1.43, +0.19] | 0.934 | -1.03% |
| trend_mom3 all | 11262 | 57.7% | 58.8 | -1.07 | [-1.56, -0.58] | 1.000 | -1.82% |
| **trend_kalshi** in-sample | 6107 | 84.8% | 85.3 | -0.54 | [-1.18, +0.11] | 0.948 | -0.63% |
| **trend_kalshi** out-of-sample | 4739 | 85.0% | 85.8 | -0.85 | [-1.66, -0.03] | 0.979 | -0.99% |
| trend_kalshi all | 10846 | 84.9% | 85.5 | -0.67 | [-1.18, -0.16] | 0.995 | -0.79% |
| **model_edge** in-sample | 2403 | 22.4% | 23.9 | -1.56 | [-2.87, -0.25] | 0.990 | -6.50% |
| **model_edge** out-of-sample | 1692 | 32.0% | 33.2 | -1.12 | [-2.97, +0.73] | 0.883 | -3.38% |
| model_edge all | 4095 | 26.4% | 27.8 | -1.38 | [-2.46, -0.29] | 0.994 | -4.96% |
| **favorite** in-sample | 6145 | 90.8% | 90.9 | -0.02 | [-0.67, +0.62] | 0.526 | -0.02% |
| **favorite** out-of-sample | 4764 | 88.4% | 90.6 | -2.22 | [-3.03, -1.40] | 1.000 | -2.45% |
| favorite all | 10909 | 89.8% | 90.7 | -0.98 | [-1.49, -0.47] | 1.000 | -1.08% |

**Verdicts** (rule in PREREGISTERED.txt, alpha = 0.010):

- `trend_open`: no edge shown
- `trend_mom3`: no edge shown
- `trend_kalshi`: no edge shown
- `model_edge`: no edge shown
- `favorite`: no edge shown

**What the pattern feels like** (all trades; a win pays the left column, a loss costs the right one):

| strategy | trades | avg win ¢ | avg loss ¢ | wins to repay 1 loss | break-even win rate | actual win rate | longest losing streak | max drawdown ¢/contract |
|---|---|---|---|---|---|---|---|---|
| trend_open | 10901 | +7.5 | -69.6 | 9.3 | 90.0% | 89.1% | 4 | 14660 |
| trend_mom3 | 11262 | +11.4 | -18.1 | 1.6 | 58.8% | 57.7% | 11 | 13234 |
| trend_kalshi | 10846 | +8.0 | -49.5 | 6.2 | 85.5% | 84.9% | 5 | 9090 |
| model_edge | 4095 | +44.3 | -17.7 | 0.4 | 27.8% | 26.4% | 37 | 7318 |
| favorite | 10909 | +7.4 | -74.1 | 10.1 | 90.7% | 89.8% | 3 | 13775 |

## Controls (no verdict)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| always_yes | 11653 | 47.0% | 48.5 | -1.46 | [-1.93, -0.98] | 1.000 | -3.00% |
| always_no | 11685 | 46.5% | 47.1 | -0.62 | [-1.10, -0.15] | 0.995 | -1.32% |

## Exploratory (no verdicts; cannot rescue a failed strategy)

### Price buckets: win rate against the price paid

| price paid | observations | avg price ¢ | win rate | 95% CI | EV after fee ¢ |
|---|---|---|---|---|---|
| 0-5¢ | 7224 | 1.4 | 1.0% | [0.8%, 1.2%] | -0.52 |
| 5-10¢ | 1542 | 7.0 | 5.8% | [4.7%, 7.0%] | -1.72 |
| 10-15¢ | 770 | 12.0 | 12.1% | [10.0%, 14.6%] | -0.74 |
| 15-20¢ | 585 | 16.8 | 17.4% | [14.6%, 20.7%] | -0.37 |
| 20-25¢ | 436 | 21.8 | 22.7% | [19.0%, 26.9%] | -0.33 |
| 25-30¢ | 385 | 27.0 | 24.4% | [20.4%, 28.9%] | -4.03 |
| 30-35¢ | 376 | 31.9 | 31.9% | [27.4%, 36.8%] | -1.55 |
| 35-40¢ | 345 | 36.9 | 34.8% | [29.9%, 40.0%] | -3.81 |
| 40-45¢ | 367 | 42.3 | 39.8% | [34.9%, 44.9%] | -4.31 |
| 45-50¢ | 321 | 47.2 | 48.0% | [42.6%, 53.4%] | -1.00 |
| 50-55¢ | 325 | 52.0 | 49.5% | [44.1%, 54.9%] | -4.31 |
| 55-60¢ | 351 | 57.0 | 57.3% | [52.0%, 62.3%] | -1.53 |
| 60-65¢ | 318 | 62.0 | 63.5% | [58.1%, 68.6%] | -0.19 |
| 65-70¢ | 360 | 67.0 | 65.3% | [60.2%, 70.0%] | -3.33 |
| 70-75¢ | 393 | 71.9 | 71.5% | [66.8%, 75.7%] | -1.94 |
| 75-80¢ | 389 | 77.0 | 74.8% | [70.3%, 78.9%] | -3.53 |
| 80-85¢ | 496 | 82.0 | 79.8% | [76.1%, 83.1%] | -3.30 |
| 85-90¢ | 676 | 87.1 | 84.5% | [81.5%, 87.0%] | -3.44 |
| 90-95¢ | 1299 | 92.4 | 91.5% | [89.9%, 92.9%] | -1.40 |
| 95-100¢ | 6380 | 98.6 | 98.7% | [98.4%, 99.0%] | +0.05 |

Each market contributes two observations (its YES and NO ask), which are mirror images, so the counts are doubled and the rows are not independent.

### model_edge threshold

| min edge | trades | win rate | mean P&L ¢ | p(mean>0) | OOS trades | OOS mean ¢ |
|---|---|---|---|---|---|---|
| 0¢ | 9093 | 46.6% | -0.97 | 1.000 | 4016 | -0.80 |
| 1¢ | 5943 | 32.4% | -1.25 | 0.999 | 2527 | -0.97 |
| 2¢ | 4827 | 28.4% | -1.39 | 0.998 | 2008 | -1.13 |
| 3¢ | 4095 | 26.4% | -1.38 | 0.994 | 1692 | -1.12 |
| 5¢ | 3058 | 24.8% | -1.06 | 0.946 | 1241 | -1.04 |
| 8¢ | 2062 | 24.1% | -0.99 | 0.883 | 832 | -1.41 |

### Entry time (mean P&L ¢ per contract, all markets)

| entry | trend_open | trend_mom3 | trend_kalshi | model_edge | favorite |
|---|---|---|---|---|---|
| 300 s left (10:00) | -0.34 (n=12294) | -1.15 (n=12306) | -0.40 (n=12176) | -2.04 (n=5100) | -0.44 (n=12294) |
| 240 s left (11:00) | -0.65 (n=12178) | -0.65 (n=12209) | -0.53 (n=12047) | -1.98 (n=5040) | -0.45 (n=12182) |
| 180 s left (12:00) | -1.07 (n=11786) | -0.86 (n=11896) | -0.82 (n=11689) | -1.81 (n=4746) | -1.06 (n=11793) |
| 120 s left (13:00) | -0.98 (n=10901) | -1.07 (n=11262) | -0.67 (n=10846) | -1.38 (n=4095) | -0.98 (n=10909) |

### Probability forecast quality

12417 markets; lower is better; always guessing 50% scores 0.2500.

| forecast | Brier score |
|---|---|
| model (gap / volatility) | 0.0710 |
| market mid-price | 0.0651 |
| 50/50 blend | 0.0666 |
