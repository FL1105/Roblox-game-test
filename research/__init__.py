"""An edge-search harness: ask "does any information we can see at decision time predict which launches are worth buying?"
in a way that cannot flatter us.

The rules it enforces (see research/README.md):
  * features see only what existed at the decision time (checked by corrupting the future and demanding identical features)
  * walk-forward, time-ordered evaluation with an embargo, never random shuffles
  * every trial is written to a ledger, and the verdict is corrected for how many were tried (White's Reality Check)
  * a final sealed recording is touched only by `confirm`, for a few frozen specs
Needs numpy, scikit-learn and lightgbm (requirements-research.txt); the trading code itself stays standard-library only.
"""
