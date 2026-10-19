# Skill gym: a self-test that does not trust its author

A seeded set of tasks, graded against the recordings in `data/snapshots/` and closed-form maths, with a ledger of scores per skill and
the level each skill has reached (the same Spark to Mastermind ladder as the skill scorecard). Standard library only. Nothing here holds a
key, a wallet or sends a transaction.

```
python -m gym prompts --seed 7 [--tier hard] --out prompts.json     # tasks for a seed; no answers in them
python -m gym grade --seed 7 --answers a.json --agent me [--reveal]  # grade and record (--reveal never works on --split sealed)
python -m gym run --agent lazy --rounds 5                            # the floor: everything is fine, every number is 0
python -m gym run --agent "cmd:python3 -m gym.claude_agent" --name claude-bot --seed 101 --rounds 1
python -m gym report
```

## What is tested

| family | what it asks | skills it exercises |
|---|---|---|
| `facts` | counts, spans and medians from a recording | duckdb-query, duckdb-read-file, sql-optimization-patterns |
| `audit` | list the flaws in a backtest script (look-ahead, overfit, multiple testing, one regime, survivorship, capacity, costs, small sample) | quantitative-research, backtesting-frameworks |
| `numbers` | break-even win rate, false-positive odds, Bonferroni, sample size, Sharpe | quantitative-research, backtesting-frameworks |
| `repro` | a finding recomputed from price paths, including a time-ordered split | backtesting-frameworks, duckdb-query |
| `rules` | COMPLY or REFUSE against this repo's own rules | repo rules |

`--tier hard` swaps in subtle versions: a centred rolling window, a scaler fitted on every row, a fee constant that is defined but never used,
a correction counted over the shortlist only, survivorship through "needs 20 quotes", clusters that are not independent, joins and data traps,
effective sample size, the expected maximum of many null Sharpe ratios, and policy cases that are one step less obvious.

## Why a single score from the author means little

The author of a test knows what was planted. So the ledger keeps three kinds of result apart:

- `author-self-test`: the person who wrote the tasks answering them. Expect near-perfect. It shows the graders work, not that the skills do.
- `claude-bot` on a `dev` seed: a fresh headless Claude per task, in a scratch directory holding only the data and the skills. Use these to find misses (`--reveal`).
- `claude-bot` on a `sealed` seed: run once, never revealed, after the dev misses were studied. This is the number to believe.

`gym/claude_agent.py` copies only `data/snapshots` and `.claude/skills` into the workspace, so the grader and the gym code are not in it. A task whose
tool calls name a path outside the workspace is voided (its answer becomes null, so it scores 0) and counted in the report.

## Honest limits

- This measures task skill on a fixed family of tasks, not general intelligence. A level here means "reached this score on these tasks".
- The tasks are generated from templates the author wrote, so a bot can still learn the templates. New seeds help; new templates help more.
- Scores do not train anything. The only learning is what is written down: `gym/lessons.md`.
- The audit tasks judge only what the code shows, using the flaw definitions in the prompt. Two reasonable reviewers can differ on a borderline script; the
  generator keeps each slot unambiguous by design, and `tests/test_gym.py` checks that every planted flaw is the only one an independent pattern finds.

## Results so far (2026-10-19)

Levels use the scorecard ladder, capped by evidence: fewer than 20 tasks behind a score caps the level at Practitioner, fewer than 50 at Specialist,
fewer than 100 at Expert. Nothing here has enough tasks for Mastermind.

**The author scoring his own test (100%, 100%) says nothing.** It shows the graders work. The first independent run did something more useful: it exposed
a flaw in the exam. A best-of-many search reported on the data that chose it is both multiple testing and in-sample, and the grader punished the bot for
saying so. The grader now accepts that second reading, and never as a substitute for the planted flaw.

**Three conditions, same fresh bot, same development seeds (101, 102; both tiers).** Mean score:

| tier | no skills | skills available | skills required |
|---|---|---|---|
| base | 97.7 | 98.6 | 94.9 |
| hard | 88.9 | 91.7 | 87.5 |

- Facts, numbers, repro and rules were 100% in every condition. Only `audit` separates anything (80, 85 and 74 over 24 tasks each).
- With skills merely available, the bot invoked **none** of them. Told to load one first, it did (quantitative-research, backtesting-frameworks,
  duckdb-read-file) and scored no better, at about 35% more cost. With 24 audit tasks per arm none of these differences is distinguishable from noise.
- The misses repeated across conditions: a correction counted over the shortlist instead of every candidate screened, a fee constant defined but never
  used, and many trades from few independent clusters. `lessons.md` states those rules in general terms.

**Sealed test of the lessons** (8 seeds nobody had looked at, hard-tier audits only, 48 tasks per arm, never revealed):

| | score | tasks fully right |
|---|---|---|
| no lessons | 88.5 | 39 / 48 |
| with `lessons.md` | 92.0 | 41 / 48 |

Paired difference +3.5 points, 95% bootstrap interval -6.2 to +13.2; better on 9 tasks, worse on 6, the same on 33. **Not a demonstrated improvement.**
The lessons also describe the generator's own patterns, so even a clear gain would show transfer to new seeds of these templates, not to new kinds of flaw.

Cost of the whole experiment: about $13 of headless Claude usage.

### What would move the bar

More tasks on the audit family (the only one that is not saturated), new flaw templates the lessons do not mention, and the same A/B with more seeds
so a 5-point difference can be told from noise. Everything else is at the ceiling for this bot.
