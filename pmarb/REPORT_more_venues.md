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

## 4. Follow-up: 115-minute recording, conservative replay, and a competition check

Recording `btc3c`: 115 minutes, 8 windows settled (04:45Z-06:30Z = 00:45-02:30 New York, so **overnight hours only**), 682 usable polls,
median skew between the three venues' answers 0.30 s. All 8 windows resolved identically on Kalshi, Polymarket and Limitless.

**Pre-registered replay** (`PREREGISTERED_replay.txt`, committed before the data was examined): execute one poll (6 s) after
the signal, in the same orientation at that poll's prices, at 50% of the displayed size, only if still profitable after fees.

| scenario | signals | executed | realised |
|---|---|---|---|
| 6 s delay, 50% size (pre-registered) | 83 | 55 | **+$390.51** (payoffs 0/1/2 = 0/55/0) |
| 12 s delay, 50% size | 83 | 49 | +$333.48 |
| 6 s delay, 25% size | 83 | 54 | +$194.26 |
| no delay, full size (upper bound) | 83 | 83 | +$888.58 |

The pre-registered "still alive" test passes (positive after the delay and haircut; 55 of 83 signals, 66%, survived the delay).
Median trade: 135 sets, $3.04 net.

**Competition check (public trade tapes, nothing placed).** For each of the 55 hedges: did anyone else lift the same side at the
same or a better price between our signal and our execution?
* Kalshi (27,000-45,000 trades per window, so a heavily automated market): **50 of 55 saw no competing taker at all**; only 5 of 55
  saw at least our size taken.
* Polymarket (300-1,000 trades per window): 39 of 55 saw none; 4 of 55 saw at least our size taken.
The stale-looking quotes mostly sat there for the 6 seconds I assumed it would take me to act. That supports them being
available, but it is not proof that my orders would have filled: it says nothing about order latency, rejections or taker delays.

**Tail risk, which this recording could not test (0 disagreements in 8 windows).** The hedges deployed $350 to $2,550 per window
(mean ~$1,430). If the venues had disagreed, one window's book would have swung from about +$1,139 to -$2,088 depending on the
direction. Historically (1,200 windows) disagreement happened 1.33% of the time, 12 windows "Kalshi Up / Polymarket Down" (which
favoured these hedges on average) against 4 the other way. At 96 windows a day that is about one such window a day, so a single day
is dominated by that tail, not by the average gain of ~$56 per window.

**Why I am not extrapolating to dollars per day:** ~$56 on ~$1,430 deployed is ~4% per 15-minute window. That is large enough
that the honest default assumption is that something real is missing: orders that do not fill at the displayed price, taker
delays on Polymarket / Limitless, cancel-on-fill behaviour, different conditions in US daytime, or size that vanishes when touched.
Only a live test with small orders (which needs a Kalshi API key and a funded Polymarket wallet) can settle that.

**Status:** a documented, repeatable lead with no counter-evidence so far, on a sample of 10 settled live windows. Not a proven edge.
