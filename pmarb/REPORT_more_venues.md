# More venues, and a lead on the BTC 15-minute market (measured 2026-10-22)

Companion to `REPORT_arbitrage_research.md` and `REPORT_bankroll.md`. Read-only: no orders, no keys. Fees included.

## 1. Other venues (Kalshi and Polymarket are the two most watched, so I looked beyond them)

| venue | public data | what I found |
|---|---|---|
| PredictIt | yes (218 markets) | **Nothing.** 0 of 31 multi-outcome markets profitable after its 10% profit fee. Its Senate-Democrat price is ~4c above Kalshi/Polymarket, but the fee turns that into a -5.5c worst case. $850 cap per contract, 5% withdrawal fee. |
| Polymarket US vs international | yes (separate pool, 40,000+ markets) | **Nothing.** 206 matched games but only 47 had two-sided books on both sides; 3 gross gaps of 2c, +0.3c to +0.5c after the US fee (1.74c at 50c). US median spread 6c vs 1c international. Residents of one pool can't use the other. |
| Smarkets | yes (176 political events) | **Nothing.** Wide and thin: Senate "Democratic" 65.8/69.0c, Le Pen bid 21c / offer 54c, a few hundred pounds of size. |
| Limitless | yes (566 markets) | Its BTC/ETH 5-, 15-minute and hourly markets **mirror Polymarket's** (`externalSlug` points at the Polymarket market; mid-prices within 0.7c on average). Taker fee 0.4-3.0%, makers free, 50-share minimum, 500 ms taker delay. Adds nothing beyond Polymarket. |
| Betfair, Predict.fun, Opinion | need API keys | not checked |

## 2. BTC 15-minute: Kalshi vs Polymarket (and Limitless)

The three list the same windows but settle on different sources (Kalshi: 60 s average of CF Benchmarks BRTI; Polymarket and
Limitless: Chainlink). Final outcomes disagreed on 1.33% of 1,200 historical windows (95% CI 0.82-2.15%), and on 1 of the
8 windows I watched live (04:00Z: Kalshi Up by $2.27, the other two Down).

**What the live concurrent log shows** (20 min, 181 usable polls, answers from the three venues arriving within a median 0.29 s,
real sizes logged):

* Kalshi vs Polymarket mid-prices differ by 5.0c on average (up to 27c); Polymarket vs Limitless by 0.7c.
* **Kalshi trails Polymarket by roughly 24 seconds**: the Kalshi mid is closest (3.3c) to Polymarket's mid from 24 s earlier, against
  5.0c at zero lag; the reverse comparison gets worse with lag. Limitless follows Polymarket in real time (0.7c at lag 0).
* A hedge (YES on one venue, NO on the other) was profitable after both venues' taker fees in 86 of 181 polls, median 233 sets, up
  to 1,701. A profitable hedge was still profitable 6 s later 54 of 85 times; the median episode lasted 2 polls (12 s), the
  longest 48 s.
* 32 first-sight entries over 20 minutes: **$197 if every entry filled at the displayed size** (median $2.33, mean $6.15, best
  $28.74 per trade; median ~162 sets, ~$145 of capital). Both windows settled the same way on all three venues, so all 32 paid $1
  and realised equals modeled. With capital locked until each window closes: $30 for a $500 bankroll, $36 at $1,000,
  $172 at $5,000, $197 at $25,000.

**Why it is only a lead, not a result:**

1. It rests on 2 settled windows live. Basis risk (1.33% of windows) is tiny against a 5-10c edge but is a total loss on the
   unlucky trade, in one direction only.
2. Nothing was executed. Whether both legs fill at the displayed prices is the whole question: fills take time, makers can
   cancel, Polymarket/Limitless apply taker delays, and the gap closes in ~12 s median.
3. The historical test I pre-registered (`PREREGISTERED_band.txt`) passed on its face: 616 trades, realised +10.5c per set,
   payoffs 0/1/2 = 3/608/5, p < 0.001, still +10.5c at a 1c half-spread. **But it is not reliable evidence**: Polymarket's price
   history has a point only every ~45 s (median age 44 s at the entry time), and near the strike these binaries move 20-40c in
   15 s, so a stale price manufactures a gap. Restricting to prices under 30 s old leaves 17 trades (+9.3c, same direction, too few to conclude).
4. Earlier, my first serial-poll log (venues fetched ~4 s apart) showed 10-38c "gaps"; the concurrent log shows the realistic scale.

## 3. What it would take

A recorder for 24-48 hours of simultaneous order books across ~100-200 windows, then a pre-registered replay that assumes
latency (enter on the quote a few seconds after first sight, require the gap to persist) and a fill haircut; then paper-trade
before any money. Practical needs: Kalshi API key (orders and its live feed), a Polymarket wallet with USDC on Polygon, and
a jurisdiction where you may use both. One 2-hour recording was started when this was written.
