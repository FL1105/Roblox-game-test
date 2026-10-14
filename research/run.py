"""Command line for the lab.

    python -m research.run sets                                  # the feature sets that exist
    python -m research.run trial --features launch,tape --model lgbm_reg --policy tp30sl15 --delay 0 --q 0.1
    python -m research.run grid --features launch,tape --models ridge,lgbm_reg --policies tp30sl15,smart --delays 0,15 --qs 0.05,0.1,0.2
    python -m research.run verdict                               # Reality Check over every trial in the ledger

Every trial is recorded; the verdict is corrected for how many there are. Set RESEARCH_AGENT=name RESEARCH_BUDGET=40 to cap one worker's trials."""

from __future__ import annotations

import argparse
import itertools
import json
import sys

from .lab import Lab
from .models import MODELS
from .outcomes import POLICIES
from .registry import FEATURE_SETS


def fmt(e: dict) -> str:
    s, sp = e["summary"], e["spec"]
    return (f"{','.join(sp['features']) or '(none)':34} {sp['model']:9} d={sp['delay_s']:<4g} {sp['policy']:9} c={sp['cost']:<5g} q={sp['q']:<4g} "
            f"n={s['n_selected']:4} mean/trade {s['mean_per_trade']:+7.2f}% CI[{s['ci_lo']:+6.1f},{s['ci_hi']:+6.1f}] win {s['win_rate'] * 100:4.1f}% blind {s['baseline_per_trade']:+.2f}%")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="research.run")
    p.add_argument("cmd", choices=["sets", "trial", "grid", "verdict"])
    p.add_argument("--data", default="data/raw_main.jsonl")
    p.add_argument("--features", default="")
    p.add_argument("--model", default="lgbm_reg")
    p.add_argument("--policy", default="tp30sl15")
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--cost", type=float, default=0.0)
    p.add_argument("--q", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--note", default="")
    p.add_argument("--models", default="ridge,lgbm_reg")
    p.add_argument("--policies", default="tp30sl15")
    p.add_argument("--delays", default="0")
    p.add_argument("--qs", default="0.1")
    a = p.parse_args(argv)
    lab = Lab([a.data])
    if a.cmd == "sets":
        for name in sorted(FEATURE_SETS):
            print(name)
        print(f"models: {', '.join(MODELS)}\npolicies: {', '.join(POLICIES)}")
        return 0
    feats = [f for f in a.features.split(",") if f]
    if a.cmd == "trial":
        print(fmt(lab.evaluate(feats, a.model, a.delay, a.policy, a.cost, a.q, a.seed, a.note)))
    elif a.cmd == "grid":
        for model, policy, delay, q in itertools.product(a.models.split(","), a.policies.split(","), [float(x) for x in a.delays.split(",")], [float(x) for x in a.qs.split(",")]):
            print(fmt(lab.evaluate(feats, model, delay, policy, a.cost, q, a.seed, a.note)))
    else:
        v = lab.verdict()
        print(json.dumps({k: v[k] for k in v if k != "best"}, indent=1, default=str))
        for e in v.get("best", []):
            print(fmt(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
