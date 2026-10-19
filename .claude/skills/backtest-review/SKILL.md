---
name: backtest-review
description: Review a backtest, strategy script or research result for the flaws that make its numbers untrustworthy. Use this skill first, before answering, whenever a backtest, strategy script, trading rule, signal or reported result is shown to you for review, audit, critique or sanity-checking, or whenever someone asks whether a result can be trusted or how it could be inflated. It carries the checklist for look-ahead, overfitting, multiple testing, survivorship, regime, costs, capacity and sample size, and it should be loaded even when the review looks simple. Judges only what the code shows and returns the flaws found, or none.
---

# Backtest review

Read the script once for what it does, then walk the eight concerns below in order. A script can carry two flaws, so do not stop at the first. Judge only
what the code shows: do not invent a flaw from a name or a comment, and do not excuse one because a comment says it is fine. Never ask the user a question;
if something is ambiguous, state your assumption and answer. If the user gives a closed list of flaw codes, use exactly those; otherwise name each flaw plainly.

For each flaw give the line, why it inflates the result, and the fix. If nothing remains after the walk, say there are none (the empty list).

## The eight concerns

1. **Look-ahead (signal timing).** For every value the signal uses, ask: could this number have been known at the decision time? Flawed: anything at time t
   computed from data after t, such as a statistic over the whole sample, a window centred on t, a rank against launches that come later, a scaler
   fitted before the split, or acting at the open of the bar whose close was used. Fine: expanding or
   trailing windows, a shift by one, a running maximum, parameters fitted on training rows only, and labels that are used only for scoring.
2. **Overfitting (choosing versus scoring).** Settings must be chosen on data that the reported number does not come from. Flawed: choosing and scoring on the
   same data, or choosing on the test data. Fine: a time split with an embargo, walk-forward folds, nested folds, a spec frozen before the data was opened.
3. **Multiple testing (count every attempt).** Every variant, parameter setting and sweep is a trial. Flawed: reporting the best of many without a correction; a
   correction whose trial count covers only what survived a first pass; any count smaller than the number of things actually tried. Fine: all attempts logged and the correction run over the full count, or one frozen spec.
4. **Survivorship (how the universe was defined).** Flawed: any filter that uses what happened after the decision, such as having graduated, still trading,
   having moved up, or having enough later history. Fine: a universe defined by what was known at launch or decision time.
5. **Regime (how many separate periods).** Flawed: one short window, or overlapping windows from one session, with a broad claim that the edge holds in general.
   Fine: several periods on different days, with the worst one shown.
6. **Costs (trace the number that is reported).** Find the line that produces the reported return or profit and read its arithmetic. Flawed: fees or slippage
   that appear as a constant, a comment or a config but are not subtracted in the arithmetic that builds what is reported. Fine: costs
   inside the reported series.
7. **Capacity (does the fill match the size).** Flawed: a size that would move the price, filled at the quote with no impact. Fine: a small size capped to
   the liquidity and filled along the curve.
8. **Sample (independent units, not rows).** Count the units that are independent: launches, days, sessions. Flawed: many trades that come from few launches or
   few days, or too few trades, with a headline claim and no interval. Fine: many separate launches, and an interval that respects clustering.

## Overlaps and discipline

- Where two readings overlap, name the primary flaw. Settings picked on the test data are overfitting (and also an uncounted search); the best of many specs
  reported without correction is multiple testing (and also in-sample). The second reading is optional; the primary one is not.
- Practices that look unusual but are sound must not be flagged. Over-flagging is an error just as missing a flaw is.
- Do not rely on the names of functions or variables; read what the arithmetic does.
