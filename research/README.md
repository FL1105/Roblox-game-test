# Research harness: finding out whether any signal is real

The trading code can already replay and paper-trade. This folder answers a different question: **does any information we can see
when a launch appears predict which launches are worth buying, by more than luck and more than costs?** It is built so that a
"yes" can be trusted and a "no" is final for the information tested.

## How to make an ML system better at something (the short version)
1. **Pick the real objective and measure it honestly.** Here: net return per trade after fees and delay, not accuracy or AUC.
   (The learned model ranked well, AUC 0.8-0.9, and still lost money: ranking is not profit.)
2. **Build the scoreboard before the model.** Time-ordered splits only, an embargo so no training outcome overlaps a test period,
   and features that can only see the past. Then test the scoreboard itself against known truth (plant a signal: it must find it;
   shuffle the outcomes: it must not).
3. **Count every attempt.** Try enough things and one will look great by luck. Every trial goes in `ledger/trials.jsonl`; the
   verdict is White's Reality Check over all of them. Without this, search finds noise and calls it an edge.
4. **Spend effort on information, not on model class.** More and better data (who traded, faster entries, more days) moves results; a fancier
   algorithm on the same information mostly fits the noise.
5. **Keep a sealed final test.** One recording (`data/raw_run2.jsonl`) is opened only by `research.confirm`, for a few specs frozen
   in advance. Whatever survives that is real enough to risk money on; nothing else is.
6. **Adapt slowly, in production, with the same honesty** (the Auto-tune screen does this: clones, paired comparison, partial moves).

## Layout
- `data.py` recordings -> tokens; `Market.view(t)` is the only way to see other tokens, and only as of time t
- `outcomes.py` what buying at the decision time returns under each exit policy (the engine's fees and delay; quiet tokens still cost fees)
- `context.py`, `features.py`, `registry.py`, `plugins/` feature sets; a plugin sees a `Context` and nothing else
- `sandbox.py` plugins that read files, import the loaders or poke at internals are rejected before they run
- `lab.py` walk-forward trials, the corrupted-future check, the verdict
- `stats.py` block bootstrap interval, Reality Check; `ledger.py` the trial log; `models.py` the scorers
- `confirm.py` freeze a spec, then test it once on the sealed recording

## Adding a feature set
```python
# research/plugins/my_idea.py
import math
from research.registry import feature_set

@feature_set("my_idea")
def my_idea(c):                 # c is a research.context.Context
    return {"something": ...}   # floats; the same keys for every token
```
Then `python -m research.run trial --features my_idea,launch --model lgbm_reg --q 0.1`. The first use runs the corrupted-future check.

## What counts as a finding
A trial counts only if (a) at least 30 trades, (b) the block-bootstrap interval of mean return per trade is above zero, (c) the Reality
Check over every trial ever run has p < 0.05, and (d) it then survives `research.confirm` on the sealed recording. Anything less is "no edge shown".
