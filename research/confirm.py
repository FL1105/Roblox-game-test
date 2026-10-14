"""The sealed test. A recording nobody has looked at is opened only here, only for specs frozen beforehand, at most MAX_SPECS of them.

    python -m research.confirm freeze --name cand1 --features launch,tape --model lgbm_reg --policy tp30sl15 --delay 0 --q 0.1 --note "why"
    python -m research.confirm run research/frozen/cand1.json --holdout data/raw_run2.jsonl

`freeze` records the exact spec, the hash of every feature file and of the harness itself, and the state of the ledger (how many dev trials
were tried, what the Reality Check said). `run` trains on ALL of the development recording, applies the model to the sealed one, and reports
one number with its interval. Each frozen spec can be confirmed once; the log is append-only."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import sys
import time

import numpy as np

from . import models, stats
from .data import load
from .lab import K_BLOCKS, MIN_TRAIN, Lab
from .registry import FEATURE_SETS, file_hash
from .outcomes import POLICIES

MAX_SPECS = 3
FROZEN_DIR = "research/frozen"
LOG = "research/holdout_log.jsonl"


def harness_hash() -> str:
    h = hashlib.sha256()
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "*.py"))):
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def freeze(args) -> str:
    lab = Lab([args.data])
    feats = sorted(f for f in args.features.split(",") if f)
    spec = lab.spec(feats, args.model, args.delay, args.policy, args.cost, args.q, args.seed)
    verdict = lab.verdict()
    frozen = {"name": args.name, "spec": spec, "harness_hash": harness_hash(), "note": args.note,
              "dev_trials": verdict.get("trials", 0), "dev_reality_check": verdict.get("reality_check"), "frozen_at": time.time()}
    os.makedirs(FROZEN_DIR, exist_ok=True)
    path = os.path.join(FROZEN_DIR, f"{args.name}.json")
    if os.path.exists(path):
        raise SystemExit(f"{path} already exists: frozen specs are never overwritten")
    with open(path, "w") as f:
        json.dump(frozen, f, indent=1)
    return path


def log_entries() -> list[dict]:
    if not os.path.exists(LOG):
        return []
    with open(LOG) as f:
        return [json.loads(line) for line in f if line.strip()]


def run(args) -> dict:
    with open(args.spec_file) as f:
        frozen = json.load(f)
    spec = frozen["spec"]
    done = log_entries()
    for e in done:
        if e["name"] == frozen["name"]:
            print("this spec was already confirmed; its recorded result:")
            return e
    if len({e["name"] for e in done}) >= MAX_SPECS:
        raise SystemExit(f"the sealed recording has already been used for {MAX_SPECS} specs")
    if frozen["harness_hash"] != harness_hash():
        raise SystemExit("the harness changed since this spec was frozen; refreezing is a new, counted decision")
    dev = Lab([args.data])
    for name, h in spec["set_hashes"].items():
        if name not in FEATURE_SETS or file_hash(FEATURE_SETS[name].__code__.co_filename) != h:
            raise SystemExit(f"feature set '{name}' changed since freezing")
    hold_tokens = load([args.holdout], unseal=True)
    hold = Lab(tokens=hold_tokens, ledger_dir="/tmp/holdout_ledger", cache_dir="/tmp/holdout_cache")
    feats = spec["features"]
    delay, policy, cost, q = spec["delay_s"], spec["policy"], spec["cost"], spec["q"]
    # train on every launch of the development recording whose outcome was decided
    Xd, _ = dev.design(feats, delay)
    yd = dev.outcomes.returns(policy, delay, cost)
    ok = np.where(~np.isnan(yd))[0]
    fit_predict = models.make(spec["model"], spec["seed"])
    Xh, _ = hold.design(feats, delay)
    yh = hold.outcomes.returns(policy, delay, cost)
    ok_h = ~np.isnan(yh)
    block = np.arange(hold.n) * K_BLOCKS // hold.n
    selected = np.zeros(hold.n, dtype=bool)
    for j in range(K_BLOCKS):
        idx = np.where(ok_h & (block == j))[0]
        if len(idx) == 0:
            continue
        scores = fit_predict(Xd[ok], yd[ok], Xh[idx])
        k = max(1, int(round(q * len(idx))))
        selected[idx[np.argsort(-scores, kind="stable")[:k]]] = True
    vals = np.where(selected, np.nan_to_num(yh), 0.0)
    lo, hi = stats.ratio_ci(vals, selected, seed=1)
    ids = stats.chunk_ids(hold.n)
    nb = ids.max() + 1
    s = np.bincount(ids, weights=vals, minlength=nb)
    c = np.bincount(ids, weights=selected.astype(float), minlength=nb)
    idx = np.random.default_rng(2).integers(0, nb, size=(stats.DRAWS, nb))
    den = c[idx].sum(1)
    boot = s[idx].sum(1)[den > 0] / den[den > 0]
    result = {"name": frozen["name"], "spec_id": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16], "holdout_launches": hold.n,
              "holdout_tradeable": int(ok_h.sum()), "n_selected": int(selected.sum()), "mean_per_trade": float(np.mean(yh[selected])) if selected.any() else float("nan"),
              "ci_lo": lo, "ci_hi": hi, "p_mean_le_zero": float((boot <= 0).mean()), "blind_per_trade": float(np.mean(yh[ok_h])), "win_rate": float(np.mean(yh[selected] > 0)) if selected.any() else float("nan"),
              "bonferroni_alpha": 0.05 / MAX_SPECS, "dev_trials_when_frozen": frozen["dev_trials"], "time": time.time()}
    with open(LOG, "a") as f:
        f.write(json.dumps(result) + "\n")
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="research.confirm")
    p.add_argument("cmd", choices=["freeze", "run"])
    p.add_argument("spec_file", nargs="?")
    p.add_argument("--data", default="data/raw_main.jsonl")
    p.add_argument("--holdout", default="data/raw_run2.jsonl")
    p.add_argument("--name"), p.add_argument("--features", default=""), p.add_argument("--model", default="lgbm_reg"), p.add_argument("--policy", default="tp30sl15")
    p.add_argument("--delay", type=float, default=0.0), p.add_argument("--cost", type=float, default=0.0), p.add_argument("--q", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=0), p.add_argument("--note", default="")
    a = p.parse_args(argv)
    if a.cmd == "freeze":
        print("frozen:", freeze(a))
    else:
        print(json.dumps(run(a), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
