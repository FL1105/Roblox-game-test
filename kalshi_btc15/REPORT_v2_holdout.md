> **v2 holdout report**: the single pre-registered scoring pass (code frozen at commit af6fc6e).

# Kalshi KXBTC15M v2: stacked model on a fresh holdout

DEV (fit, seen before): 7023 markets with all inputs. HOLDOUT (never seen): 5383 markets with all inputs (5424 sampled, 5424 downloaded).

## Fitted on DEV (b1 = 1 with every other row 0 would mean the market is already perfectly calibrated and complete)

| term | coefficient | std error | z |
|---|---|---|---|
| intercept | -0.042 | 0.045 | -0.9 |
| market (logit mid) | +0.944 | 0.090 | +10.5 |
| gap z-score | +0.422 | 0.257 | +1.6 |
| market momentum (3 min) | -0.271 | 0.221 | -1.2 |
| last trade vs mid | -0.014 | 0.034 | -0.4 |
| BTC momentum (3 min, sigma units) | +0.240 | 0.142 | +1.7 |

Features other than the market are standardised on DEV, so their coefficients are per one standard deviation. Ridge penalty 1.0 on those four.

## H_F: does the stack add information? (HOLDOUT, scored once)

| forecast | log loss | Brier | Δ log loss vs raw mid (paired) | p(better) |
|---|---|---|---|---|
| raw mid-price | 0.1852 | 0.0558 |  |  |
| calibrated market (x1 only) | 0.1860 | 0.0558 | +0.00082 ± 0.00022 | 0.9999 |
| stacked model | 0.1863 | 0.0560 | +0.00116 ± 0.00045 | 0.9946 |

**Verdict H_F:** no information added (pre-registered: p < 0.01 against the raw mid-price).

## H_T: does it make money after the spread and fee? (HOLDOUT)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| stack_edge in-sample (DEV) | 194 | 43.8% | 43.7 | +0.16 | [-6.33, +6.66] | 0.480 | +0.37% |
| **stack_edge HOLDOUT** | 64 | 28.1% | 31.5 | -3.37 | [-14.02, +7.28] | 0.732 | -10.70% |

**Verdict H_T:** no verdict: 64 out-of-sample trades, need 300

### Threshold sensitivity (exploratory, holdout)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| stack_edge ≥ 0¢ | 2155 | 9.9% | 11.3 | -1.32 | [-2.25, -0.39] | 0.997 | -11.74% |
| stack_edge ≥ 1¢ | 264 | 17.8% | 21.3 | -3.51 | [-7.94, +0.93] | 0.939 | -16.45% |
| stack_edge ≥ 2¢  (pre-registered) | 64 | 28.1% | 31.5 | -3.37 | [-14.02, +7.28] | 0.732 | -10.70% |
| stack_edge ≥ 3¢ | 16 | 37.5% | 37.1 | +0.41 | [-21.54, +22.36] | 0.486 | +1.10% |
| stack_edge ≥ 5¢ | 2 | 50.0% | 39.2 | +10.80 | [-86.22, +107.82] | 0.414 | +27.55% |
| stack_edge ≥ 8¢ | 0 | 0.0% | 0.0 | +0.00 | [+0.00, +0.00] | 1.000 | +0.00% |

### The five v1 strategies on the same holdout (no verdict here; the final v1 verdict is its own run)

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| trend_open | 4564 | 90.2% | 90.3 | -0.08 | [-0.82, +0.66] | 0.583 | -0.09% |
| trend_mom3 | 4765 | 57.0% | 58.3 | -1.30 | [-2.01, -0.58] | 1.000 | -2.22% |
| trend_kalshi | 4544 | 85.4% | 86.0 | -0.56 | [-1.31, +0.18] | 0.931 | -0.65% |
| model_edge | 1870 | 19.0% | 21.1 | -2.07 | [-3.52, -0.61] | 0.997 | -9.80% |
| favorite | 4573 | 90.9% | 91.0 | -0.05 | [-0.79, +0.69] | 0.554 | -0.06% |

### stack_edge by month (holdout)

| month | trades | win rate | mean P&L ¢ |
|---|---|---|---|
| 2026-01 | 5 | 60.0% | +25.08 |
| 2026-02 | 5 | 20.0% | -11.98 |
| 2026-03 | 6 | 50.0% | +7.80 |
| 2026-04 | 5 | 20.0% | -3.76 |
| 2026-05 | 10 | 20.0% | -9.06 |
| 2026-06 | 6 | 0.0% | -19.83 |
| 2026-07 | 14 | 28.6% | -3.63 |
| 2026-08 | 13 | 30.8% | -3.75 |

## In-sample (DEV) trading, informational

| strategy / split | trades | win rate | avg cost ¢ | mean P&L ¢/contract | 95% CI | p(mean>0) | ROI |
|---|---|---|---|---|---|---|---|
| stack_edge (DEV) | 194 | 43.8% | 43.7 | +0.16 | [-6.33, +6.66] | 0.480 | +0.37% |
