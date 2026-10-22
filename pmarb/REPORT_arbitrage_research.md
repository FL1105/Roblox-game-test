# Prediction-market arbitrage: what exists on Kalshi and Polymarket (measured 2026-10-21/22)

Read-only measurement. No orders were placed, no keys were used. All profit figures are after taker fees on every leg
(Kalshi quadratic 0.07 with per-series multipliers; Polymarket per-market `feeSchedule`; makers are free except on the 160 Kalshi `quadratic_with_maker_fees` series), sized to top-of-book depth.

## Bottom line

**No risk-free arbitrage that a retail trader could capture was found.** Both venues are priced tightly, internally and
against each other. The only positive-after-fee events were brief in-play dislocations between the two venues in live games.

## 1. Within one venue (full snapshot, every open market)

| check | Kalshi | Polymarket |
|---|---|---|
| events / quotes scanned | 15,303 / 159,747 | 22,724 / 274,597 |
| mutually exclusive set, buy NO on every outcome | **0 of 4,343** profitable | **0 of 6,725** profitable |
| threshold ladders (a stricter condition priced above a looser one) | **0 of 317,280** pairs profitable | no strike data |
| buy YES on every outcome | 77 "profitable" | 183 "profitable" |

The "buy every outcome" hits are **not arbitrage**: they only mean the listed outcomes sum to less than $1, which happens
whenever the list is incomplete (for example 47 named candidates in the Republican 2028 nominee market, with the rest
unlisted). Neither API says whether a list is exhaustive.

Two bugs in my own first pass produced fake profit ($728k "risk-free", then 12 Starship-launch "arbs"): comparing
thresholds across different players in one event, and treating Kalshi's exact-count bins as a ladder. Both are fixed
and covered by tests (`tests/test_pmarb.py`).

## 2. Across venues, same proposition

* **Identical-rule, high-liquidity markets agree to the cent.** Fed October hold: Kalshi 88/89c vs Polymarket 87/88c.
  Fed 25bp hike: 11/12 vs 12/13. Senate control (D): 65/66 vs 66/67. House control (D): 91.2/91.3 vs 91/92. Best buy on one venue
  plus best sell on the other costs exactly $1.00 in each case, with $100k+ of size at the touch.
* **Games, exact matching** (date + both team codes): 204 games, 308 team-sides. Gross gap before fees in only 9, at most 1c;
  **0 profitable after fees** in the snapshot.
* **Fuzzy matching of other markets is unsafe**: 7,145 title-matched pairs produced "+97% arbitrage" from complements,
  different election rounds and reversed scorelines. After reading the rules of the few plausible survivors:
  * Topuria's next opponent: not the same proposition (Kalshi: first fight he actually competes in; Polymarket: next fight officially announced).
  * Le Pen winning the next French presidential election: same proposition. Kalshi 52/53c vs Polymarket 46.3/46.4c. Buying
    Polymarket YES + Kalshi NO costs ~94.4c for a $1 payout, ~3.9c net per set after fees on ~1,000 sets. It pays out around
    April-May 2027: roughly 4% in about 6.5 months (~7-8% a year) with capital tied up on two venues. That is carry, about the
    risk-free rate plus a little, not a free lunch.
* **BTC 15-minute (Kalshi vs Polymarket):** different settlement sources (CF Benchmarks BRTI 60 s average vs Chainlink
  60 s TWAP) and start references. Outcomes disagreed on **16 of 1,200 windows (1.33%, 95% CI 0.82-2.15%)**, so a hedge across
  them can pay $0 or $2. Live gaps (up to 9c) are the market pricing that difference, not arbitrage.

## 3. Forty minutes of polling (119 polls, 306 game sides + 4 BTC windows, every 20 s)

| | games | BTC 15m |
|---|---|---|
| observations | 36,414 | 119 |
| gross gap > 0 before fees | 1,538 (best 12c) | 52 (best 9c) |
| net > 0 after fees, taker both legs | **72 observations, 44 episodes** | 28 (basis risk, see above) |
| theoretical profit if every episode were taken once at its best moment | $157 (upper bound) | not riskless |
| longest episode | 9 polls (3 min), most last 1-2 polls | |

The largest game gap (MIL/WAS, 6c, 595 sets, $25 net) was a real live-game moment: MIL's win probability jumped 42% to 55%
in three minutes, Polymarket repriced first (54/55c) while Kalshi still showed 47/48c, and Kalshi caught up within 20 s.
These need both legs filled at the displayed size almost simultaneously, which polling at 20 s cannot test. Treat $157 as
a ceiling: fills, latency, queue position and legging risk (one side executes, the other moves) are all unmeasured.

A simple lead-lag rule (trade the lagging venue toward the other after a gap) lost money with tight books: 2c gap, -3.5c
per contract on Kalshi and -1.0c on zero-fee Polymarket; the 3c and 4c cases had n = 48 and n = 11 and are indistinguishable from zero.

## What would have to be true for this to be worth building

1. A real-time feed, not 20 s polling. Polymarket's market WebSocket is public; Kalshi's requires an API key.
2. Funded accounts on both venues and execution that fills both legs within a second or two.
3. Eligibility: Polymarket's international site restricts US persons; check terms before funding anything.
4. Evidence that fills at the displayed size are achievable. That can only be measured by recording order-book updates and
   replaying them, or by paper-trading against live books.

Without those, the honest answer is that the free money is already taken.
