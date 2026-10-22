# Bankroll size and arbitrage: what changes, what doesn't (measured 2026-10-21/22)

Companion to `REPORT_arbitrage_research.md`. Same rules: read-only, no orders, all figures after fees.
Regenerate the tables with `python -m pmarb bankroll --log <watch log>`.

## Short answer

Bankroll size does not turn a non-arbitrage into one. It changes three things: **how much of the fee is wasted on rounding**
(small orders), **how much the order books can absorb** (large orders), and **how much capital is locked** while you wait.
The sweet spot for what exists is roughly **$1,000 to $25,000**, and even there the profit is a few percent a year above
cash for the carry trade, and a low, unproven ceiling for the live-game dislocations.

## 1. Small bankrolls pay more fee per contract

Kalshi rounds the **whole order's** fee up to the next cent, so a 1-contract order pays at least 1c. Taker fee per contract, cents:

| price | 1 | 5 | 10 | 25 | 100 | 1000 | formula (no rounding) |
|---|---|---|---|---|---|---|---|
| 0.05 / 0.95 | 1.00 | 0.40 | 0.40 | 0.36 | 0.34 | 0.33 | 0.33 |
| 0.30 / 0.70 | 2.00 | 1.60 | 1.50 | 1.48 | 1.47 | 1.47 | 1.47 |
| 0.50 | 2.00 | 1.80 | 1.80 | 1.76 | 1.75 | 1.75 | 1.75 |
| 0.90 | 1.00 | 0.80 | 0.70 | 0.64 | 0.63 | 0.63 | 0.63 |
| 0.97 | 1.00 | 0.40 | 0.30 | 0.24 | 0.21 | 0.20 | 0.20 |

* The overhead is worst at extreme prices: at 97c a 1-contract order pays **~390% more** than the formula; at 10 contracts +47%;
  at 100 contracts +3%.
* At mid prices it is mild (+14% at 1 contract, +3% at 10, none at 100).
* Rule of thumb: **about 100 contracts per leg (roughly $50 at 50c, $97 at 97c) removes the rounding drag.** Below that, you
  pay extra fee for nothing.
* Polymarket has no rounding, but a **5-share minimum** per order, and the bankroll must be split across two venues.

Break-even gross gap per set (taker on a Kalshi leg, zero-fee Polymarket leg): about **1.75c at 50c**, 0.63c at 10c/90c,
0.2c at 97c. Two fee-paying legs (for example Kalshi + Polymarket crypto at 0.07) double it to 3.5c at 50c.

**Fee-free options exist but nothing on them is arbitrage:** 14 Kalshi series have a zero multiplier (Iran, Greenland, annual GDP,
BTC/ETH year-end, Trump-out) and Polymarket geopolitics is fee-free. The overlapping pairs I found were not the same
proposition (for example Kalshi's "Pahlavi leads Iran" counts being *named* head of state; Polymarket requires de facto control,
so the valid hedge direction costs $1.015).

**Correction to my earlier report:** makers are *not* always free. 160 Kalshi series are `quadratic_with_maker_fees`. Makers are free
on Polymarket and on plain `quadratic` Kalshi series (for example NBA games).

## 2. The carry trade: Le Pen (French presidential election)

Same proposition on both venues (rules read: "wins the next French presidential election", two-round, includes an early election).
Buy Polymarket YES + Kalshi NO, walking both live order books; Polymarket is fee-free here, Kalshi's fee is on its NO leg:

| sets | capital | net after fees | net per set | return |
|---|---|---|---|---|
| 100 | $94 | $4.75 | 4.75c | 5.1% |
| 1,000 | $942 | $40.46 | 4.05c | 4.3% |
| 10,000 | $9,447 | $378 | 3.78c | 4.0% |
| 25,000 | $23,760 | $803 | 3.21c | 3.4% |
| 50,000 | $55,010 | **-$5,821** | | the books run out and the marginal sets lose money |

* The return holds at ~4-5% from $100 to ~$25,000, then collapses: **more bankroll helps only until the order books are
  exhausted (about $25k here).** Top-of-book alone offered ~$40 on 1,000 sets; walking deeper levels is what scales it.
* It pays out around April-May 2027: ~4-5% over ~6.5 months, **roughly 8-9% a year**, on capital tied up on two venues.
  Not included: interest or holding rewards either venue may pay on balances or positions, deposit/withdrawal costs, or
  the cost of the money. The opposite direction (Kalshi YES + Polymarket NO) loses at every size.
* The rest of the French field gives smaller gaps in both directions (Hollande 2.1%, Lisnard 1.5%, Lagarde 1.2%,
  Melenchon 1.1%, Philippe 1.1%, Bardella 0.7%, Attal 0.2% at top of book). The US 2028 nominee markets show 0.2-2%, but those
  resolve in 2028, so they are worth less than cash.
* Risk that sizing does not remove: the two venues could resolve differently (rules, cancellation, an annulled election),
  and either venue could fail or freeze withdrawals for six months.

## 3. Live-game dislocations (40-minute watch)

First-sight entry on every profitable episode, order sized to the displayed depth and to half the bankroll on each venue,
**capital locked until the games end**, instant fills assumed (so this is a ceiling):

| bankroll | trades | net | return in 40 min |
|---|---|---|---|
| $100 | 1 | $1.98 | 2.0% |
| $500 | 3 | $4.82 | 1.0% |
| $1,000 | 3 | $6.24 | 0.6% |
| $5,000 | 7 | $23.64 | 0.5% |
| $10,000 | 15 | $39.55 | 0.4% |
| $25,000 | 41 | $155.81 | 0.6% |

* Bigger bankrolls capture more episodes (41 at $25k vs 3 at $1k) because capital is locked per game; the **return on capital
  does not improve**, it stays around 0.4-0.6% per 40 minutes at best.
* Without the lock (capital reusable instantly) the same log gives $136 at $1,000 and $146 at $2,500+. The difference between
  that and $6 is how much a small bankroll loses by waiting for each game to finish.
* The median profitable episode was worth $0.14-$2.50. A handful of larger ones carry the total. The best single moment
  ($25 on 595 sets) lasted under 20 seconds.
* What the numbers do **not** include: latency, partial fills, the other side moving after the first leg fills, queue position,
  the fact that polling every 20 s sees only some moments, and that 34 of the 72 profitable observations were esports and hockey
  with a fee on both venues (inferred fee multiplier ~1.8).

## 4. Practical bankroll tiers

| bankroll | what is realistic |
|---|---|
| under ~$100 | Rounding and the 5-share minimum eat the edge. Not viable. |
| $100 - $1,000 | The carry trade works at full return but earns single to low double dollars. Live-game capture is a few dollars at best. |
| $1,000 - $25,000 | The useful range. Carry earns ~4-5% on the capital over ~6.5 months; game dislocations scale with capital but not in return. |
| over ~$25,000 | Depth-limited. Returns fall and then go negative as you walk the books. |

None of this is risk-free in practice: it all assumes the displayed prices and sizes are available when you act, and nothing was executed.
