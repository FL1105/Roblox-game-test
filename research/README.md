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
- `data.py` recordings -> tokens; `Market.view(t)` is the only way to see other tokens, and only as of time t; the data fingerprint covers every field
- `outcomes.py` what buying at the decision time returns under each exit policy (the engine's fees and delay; a quiet token still costs fees; no hidden engine gates)
- `context.py`, `features.py`, `registry.py`, `plugins/` feature sets; a plugin sees a `Context` and nothing else
- `sandbox.py` static rules for plugins: no files, no loaders, no state kept between calls (no mutable defaults, module containers or attribute writes),
  one parameter, no answer keys (no long string constants, no big tables)
- `worker.py` every feature set is built in a FRESH process in decision-time order and must pass behavioural checks: pure (same numbers again, reversed,
  shuffled), unchanged when everything after the decision time is scrambled, unchanged (and the whole Context identical) when it is deleted
- `lab.py` walk-forward trials, the family guard, the verdict; `stats.py` block bootstrap interval, scaled Reality Check; `ledger.py` the trial log
- `versioning.py` one code hash over the harness, the engine and the data conversion: it is in every cache key, ledger entry and frozen spec
- `confirm.py` freeze a spec (it must already be a counted development trial), then test it once on the sealed recording

## The evaluation universe
One pool of launches for every policy, defined only by things known at the decision time or by launch time: a token needs a quote on its curve, and launches in the
last 12 minutes of each recording file are cut (their life was truncated by the recorder stopping). Nothing is dropped for what the token later did.
Known limits, not hidden: tokens whose quotes were off the standard curve at the first look (~6%, the hottest launches, probably already graduated) cannot be
priced and are excluded; a token that went quiet for 75 s is assumed flat afterwards (some would have moved again); the first price arrives ~13 s after launch, so
nothing here says anything about faster entries; the simulator has no competing bots and always fills.

## Adding a feature set
```python
# research/plugins/my_idea.py
import math
from research.registry import feature_set

@feature_set("my_idea")
def my_idea(c):                 # c is a research.context.Context
    return {"something": ...}   # floats; the same keys for every token; pure: no state
```
Then `python -m research.run trial --features my_idea,launch --model lgbm_reg --q 0.2`. The first use builds the matrix in a worker and runs all the checks.

## What counts as a finding
A trial counts only if (a) at least 60 trades and a 99% block-bootstrap interval above zero, (b) it was run with a transaction cost of at least 0.001 SOL per side,
(c) it is the very trial that drives the Reality Check, over every comparable trial in the ledger, and that has p < 0.05 after inflating for trials from older
data or rules, and (d) it then passes `research.confirm` on the sealed recording: at least 100 trades and a one-sided p below 0.05 / 3.
Changing the data starts a new family only by an acknowledged, logged reset (`RESEARCH_ACK_NEW_DATA`); old trials stay counted. Anything less is "no edge shown".
