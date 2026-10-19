# Review lessons

Written from the audit tasks the bot got wrong on development seeds, in general terms with no seed-specific answers. Apply them when reviewing a
backtest. Judge only what the code shows.

1. **Count every attempt, including the ones thrown away before the formal test.** If a correction (Reality Check, Bonferroni, Sidak) is given a
   trial count, ask where the count comes from. A count taken after a pre-screen ("specs that passed a first look") covers the survivors only; the
   correction must cover every candidate that was looked at. Under-counted trials are a multiple-testing flaw even when a correction is present.
2. **A defined but unused constant is not a cost model.** Trace the line that produces the returns. If fees or slippage appear in a constant, a
   comment or a config but not in the arithmetic that builds `ret`, the returns are gross of costs.
3. **The sample is the number of independent units, not the number of rows.** If hundreds of trades come from a handful of launch clusters, sessions
   or days, the effective sample is the cluster count. A large trade count over few clusters, with a headline claim and no interval, is too small a sample.
4. **Look at all eight concerns every time, and do not stop at the first finding.** A script can carry two flaws. Walk them in order: when the signal
   could be known, how settings were chosen versus scored, how many variants were tried and counted, how the universe was defined, how many separate
   periods were used, whether costs are in the arithmetic, whether the size fits the liquidity, and how many independent units there are.
5. **Do not flag what the code does not show.** A time split with an embargo, a walk-forward, nested folds, a spec frozen in advance, costs inside
   the return line, a size capped to the pool and filled along the curve, and several recordings on different days are all fine. If nothing is left
   after the walk, the answer is the empty list.
6. **Where two readings overlap, name the primary flaw.** Settings picked on the test data are overfitting (and also an uncounted search). The best
   of many specs, reported without correction, is multiple testing (and also in-sample). The second reading is optional; the primary one is not.
7. **A universe that uses later information keeps survivors.** Filters such as "graduated", "still trading", "moved up", or "needs N quotes of
   history" condition on what happened after the decision.
