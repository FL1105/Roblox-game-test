# Evaluation on real recorded launches
generated 2026-10-14 03:10 from 1 file(s); 1154 priceable launches over 100 minutes; 807 earlier launches for fitting, 347 later ones held out

## What happens to a blindly sniped launch
```
1154 priceable tokens, 1134 with at least one observation
how tracking ended: {'idle': 1002, 'graduated': 46, 'window_over': 54, 'open': 32}
first quotable 13.3s after launch (median)
price moved at least once after the first quote: 563/1134 (50%)
already above launch price at first quote by >10%: 312/1134

BLIND ENTRY at the first quote, 0.1 SOL, net of fees. Peak = best moment in hindsight, not achievable:
  peak net gain >= +10%: 44/1134 (4%)
  peak net gain >= +30%: 27/1134 (2%)
  peak net gain >= +50%: 18/1134 (2%)
  peak net gain >= +100%: 7/1134 (1%)
  held for a fixed time then sold (tokens still observed at that time):
    after  15s: n=1116  mean   -3.2%  median   -3.1%  winners   0%
    after  30s: n=1102  mean   -3.4%  median   -3.1%  winners   1%
    after  60s: n=1079  mean   -4.6%  median   -3.1%  winners   3%
    after 120s: n=1070  mean   -5.3%  median   -3.1%  winners   4%
    after 300s: n=1064  mean   -6.7%  median   -3.1%  winners   3%
  first to be hit, +30% or -15% net: {'neither': 969, 'tp30': 25, 'sl15': 140}
  of tokens that hit either, share hitting +30% first: 15%  (a coin flip would be 50%; with costs, break-even needs 33%+ for a 30/-15 rule)
```

## Learned hold/sell model
```
807 training launches (47797 samples), 347 test launches (17880 samples)
label: over the next 20s holding gains >3% (up) or loses >2% (down). base rates train: up 2.1%, down 5.2%; test: up 1.2%, down 4.3%
  up    head: ridge 0.3  test AUC 0.805   | best single feature on test: dev_pct AUC 0.810  (0.5 = no skill)
  down  head: ridge 0.3  test AUC 0.909   | best single feature on test: liq_sol AUC 0.885  (0.5 = no skill)
saved data/model_final.json
```

## Strategies on UNSEEN launches (the later part only)

### transaction cost 0.0 SOL per side
```
347 real launches | tx cost 0.0 SOL per side | decision-to-fill delay 800 ms | 0.1 SOL per trade
strategy                               trades    win mean ret  95% CI of mean   P&L SOL
snipe + fixed targets (30% / -15%)       186     2%   -4.66%  [ -5.7, -3.8]    -0.867
snipe + smart exit                       186     3%   -4.54%  [ -5.6, -3.6]    -0.844
confirm + fixed targets                   24     4%  -14.89%  [-21.1, -8.4]    -0.357
confirm + smart exit                      24     8%  -14.01%  [-20.6, -7.3]    -0.336
snipe + ladder (30%: half, 60%: quarter, trail 12%)   186     2%   -4.56%  [ -5.6, -3.6]    -0.847
confirm + learned model                   24     0%   -6.25%  [ -8.4, -4.3]    -0.150
snipe + learned model                    186     2%   -3.64%  [ -4.1, -3.2]    -0.677
```

### transaction cost 0.001 SOL per side
```
347 real launches | tx cost 0.001 SOL per side | decision-to-fill delay 800 ms | 0.1 SOL per trade
strategy                               trades    win mean ret  95% CI of mean   P&L SOL
snipe + fixed targets (30% / -15%)       186     1%   -6.60%  [ -7.6, -5.8]    -1.228
snipe + smart exit                       186     2%   -6.41%  [ -7.4, -5.5]    -1.192
confirm + fixed targets                   24     4%  -16.36%  [-22.7,-10.2]    -0.393
confirm + smart exit                      24     8%  -15.50%  [-22.1, -9.3]    -0.372
snipe + ladder (30%: half, 60%: quarter, trail 12%)   186     1%   -6.50%  [ -7.5, -5.5]    -1.210
confirm + learned model                   24     0%   -8.19%  [-10.3, -6.3]    -0.197
snipe + learned model                    186     1%   -5.64%  [ -6.1, -5.2]    -1.049
```

### transaction cost 0.003 SOL per side
```
347 real launches | tx cost 0.003 SOL per side | decision-to-fill delay 800 ms | 0.1 SOL per trade
strategy                               trades    win mean ret  95% CI of mean   P&L SOL
snipe + fixed targets (30% / -15%)       187     1%  -10.59%  [-11.6, -9.6]    -1.979
snipe + smart exit                       187     2%  -10.35%  [-11.4, -9.3]    -1.935
confirm + fixed targets                   24     4%  -18.58%  [-24.7,-12.4]    -0.446
confirm + smart exit                      24     8%  -17.33%  [-23.8,-10.3]    -0.416
snipe + ladder (30%: half, 60%: quarter, trail 12%)   187     1%  -10.54%  [-11.5, -9.6]    -1.970
confirm + learned model                   24     0%  -12.04%  [-14.0,-10.2]    -0.289
snipe + learned model                    187     1%   -9.68%  [-10.1, -9.2]    -1.810
```

## Which smart-exit rules matter? (unseen launches, no transaction cost)
```
smart-exit variant                     trades    win mean ret  95% CI of mean   P&L SOL
smart exit, all rules                    186     3%   -4.54%  [ -5.6, -3.6]    -0.844
  without momentum take-profit           186     3%   -4.53%  [ -5.6, -3.6]    -0.842
  without volatility trail               186     3%   -4.54%  [ -5.6, -3.6]    -0.844
  without stalled-winner exit            186     2%   -4.66%  [ -5.7, -3.7]    -0.867
  without sell-pressure exit             186     3%   -4.54%  [ -5.6, -3.6]    -0.844
  none of the four (stop loss + cap only)   186     2%   -4.43%  [ -5.5, -3.1]    -0.825
```

## Entry parameters: do any of them help? (settings picked on earlier launches, confirmed on unseen ones, no transaction cost)
```
entry filter (snipe + smart exit)              | EARLIER launches: n  mean  95% CI        | UNSEEN launches: n  mean  95% CI
baseline (no entry filter)                     |  441   -4.0% [ -4.9, -3.1]               |  186   -4.5% [ -5.6, -3.6]
min liquidity 0.5 SOL raised                   |  242   -5.2% [ -6.8, -3.6]               |  123   -5.7% [ -7.3, -4.2]
min liquidity 1 SOL raised                     |  123   -6.5% [ -9.5, -3.3]               |   44  -10.1% [-14.0, -6.2]
min liquidity 2 SOL raised                     |   82   -6.5% [-10.9, -1.8]               |   26  -13.7% [-20.1, -7.9]
min liquidity 5 SOL raised                     |   35   -6.7% [-15.2, +2.8]               |    8  -23.5%      n<20    
max dev holding 0%                             |  119   -3.7% [ -4.2, -3.3]               |  104   -3.8% [ -4.9, -3.1]
max dev holding 3%                             |  332   -3.1% [ -3.9, -2.2]               |  153   -4.0% [ -5.0, -3.2]
max dev holding 7%                             |  395   -3.8% [ -4.6, -3.0]               |  176   -4.4% [ -5.4, -3.5]
at least 1 price moves seen                    |  441   -4.0% [ -4.9, -3.1]               |  186   -4.5% [ -5.6, -3.6]
at least 2 price moves seen                    |    0   +0.0%      n<20                   |    0   +0.0%      n<20    
at least 4 price moves seen                    |    0   +0.0%      n<20                   |    0   +0.0%      n<20    
token younger than 15s when bought             |   70   -5.4% [ -8.1, -2.6]               |   20   -5.9% [ -8.9, -3.7]
token younger than 30s when bought             |  131   -5.9% [ -7.7, -4.1]               |   70   -5.3% [ -6.9, -3.8]
not already up more than 5%                    |  298   -4.1% [ -4.8, -3.4]               |  144   -3.4% [ -3.8, -2.9]
not already up more than 20%                   |  408   -3.9% [ -4.7, -3.2]               |  178   -3.7% [ -4.4, -3.0]
not already up more than 60%                   |  423   -4.1% [ -4.8, -3.3]               |  183   -4.2% [ -4.9, -3.3]
creator has at most 0 earlier launches (24h)   |  210   -3.4% [ -5.0, -1.6]               |  146   -5.2% [ -6.5, -3.9]
creator has at most 2 earlier launches (24h)   |  274   -4.1% [ -5.5, -2.7]               |  168   -4.9% [ -6.1, -3.8]
skip creators with a collapsed token           |  437   -4.0% [ -4.9, -3.0]               |  186   -4.5% [ -5.6, -3.6]
scanner PASS only                              |  319   -3.3% [ -4.4, -2.1]               |  151   -3.9% [ -5.0, -3.0]
confirm entry: up 5% with buying               |  158   -4.1% [ -6.3, -1.7]               |   70   -7.9% [-10.7, -5.5]
confirm entry: up 15% with buying              |   59   -5.2% [-11.1, +0.8]               |   17  -18.7%      n<20    
combo: 2+ moves, not up >60%, PASS only        |    0   +0.0%      n<20                   |    0   +0.0%      n<20    
combo: creator filters + dev <=3% + PASS       |  140   -2.4% [ -4.1, -0.4]               |  122   -3.6% [ -4.8, -2.8]
combo: confirm 5% + not up >60% + 1+ move      |  139   -4.2% [ -6.0, -2.4]               |   66   -7.1% [ -9.4, -5.0]

Selected on the earlier launches (best mean with >= 30 trades): combo: creator filters + dev <=3% + PASS
  earlier: -2.37% over 140 trades     unseen: -3.59% over 122 trades   (baseline unseen: -4.54% over 186)
  -> on unseen launches the selected filter does NOT clearly hold up (needs 20+ trades and a confidence interval entirely above the baseline's mean)
24 settings were tried. A few will look good by chance alone; only an improvement that appears on BOTH sides, with a clear margin, means anything.
```

## For reference: the same strategies on the EARLIER launches (the model was fitted here, so this flatters it)
```
809 real launches | tx cost 0.0 SOL per side | decision-to-fill delay 800 ms | 0.1 SOL per trade
strategy                               trades    win mean ret  95% CI of mean   P&L SOL
snipe + fixed targets (30% / -15%)       440     5%   -4.05%  [ -5.1, -3.0]    -1.782
snipe + smart exit                       441     6%   -4.02%  [ -4.9, -3.1]    -1.774
confirm + fixed targets                   74    19%   -6.15%  [-11.8, -0.2]    -0.455
confirm + smart exit                      74    26%   -5.09%  [ -9.9, +0.0]    -0.376
snipe + ladder (30%: half, 60%: quarter, trail 12%)   439     6%   -3.96%  [ -4.9, -2.9]    -1.736
confirm + learned model                   74     9%   -4.66%  [ -6.5, -2.7]    -0.345
snipe + learned model                    442     3%   -3.66%  [ -4.1, -3.2]    -1.618
```

## How to read this
- `mean ret` is the average net return per trade after fees and costs; the 95% interval is a bootstrap and needs 20+ trades to appear.
- A strategy only counts if its interval on UNSEEN launches sits above zero, at a cost level you can actually achieve.
- The recorder cannot see who traded, so creator-sell exits cannot fire, and entries come several seconds after launch.
- A few hundred launches is a small sample: treat anything short of a clear, repeated win as 'no edge shown'.
