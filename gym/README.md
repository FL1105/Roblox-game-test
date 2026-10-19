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
