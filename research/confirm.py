"""The sealed test. A recording nobody has looked at is opened only here, only for specs frozen beforehand, at most MAX_SPECS of them.

    python -m research.confirm freeze --name cand1 --features launch,tape --model lgbm_reg --policy tp30sl15 --delay 0 --q 0.2 --note "why"
    python -m research.confirm run research/frozen/cand1.json --holdout data/raw_run2.jsonl,data/raw_run2b.jsonl

`freeze` records the exact spec (which must already exist as a development trial in the ledger), a digest of it, the code hash of the harness and of
the engine/data code, and the state of the ledger. `run` verifies all of that, trains on ALL of the development recording, applies the model to the
sealed one, and writes one verdict with its numbers to an append-only log. Each spec can be confirmed once, whatever directory you run from.
A spec passes only if it traded at least MIN_TRADES times and the one-sided block-bootstrap p-value is below 0.05 / MAX_SPECS."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time

import numpy as np

from . import models, stats
from .lab import K_BLOCKS, Lab
from .ledger import spec_id
from .paths import DEV_DATA, FROZEN_DIR, HOLDOUT_LOG, LEDGER_DIR
from .registry import SOURCE_HASH
from .versioning import code_hash

MAX_SPECS = 3
MIN_TRADES = 100
LOG = HOLDOUT_LOG
EMBARGO_MS = 15 * 60_000


def digest(frozen: dict) -> str:
    body = {k: frozen[k] for k in ("spec", "code", "dev_fp") if k in frozen}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def freeze(args) -> str:
    lab = Lab(args.data.split(","), ledger_dir=getattr(args, "ledger_dir", None) or LEDGER_DIR)
    feats = sorted(f for f in args.features.split(",") if f)
    spec = lab.spec(feats, args.model, args.delay, args.policy, args.cost, args.q, args.seed)
    if lab.ledger.get(spec_id(spec)) is None:
        raise SystemExit("this exact spec has not been run as a development trial: run it first (it counts toward the verdict), then freeze it")
    verdict = lab.verdict()
    frozen = {"name": args.name, "spec": spec, "spec_id": spec_id(spec), "code": code_hash(), "dev_fp": lab.fp, "note": args.note,
              "dev_trials": verdict.get("total_trials", 0), "dev_reality_check": verdict.get("reality_check"), "frozen_at": time.time()}
    frozen["digest"] = digest(frozen)
    directory = getattr(args, "frozen_dir", None) or FROZEN_DIR
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{args.name}.json")
    if os.path.exists(path):
        raise SystemExit(f"{path} already exists: frozen specs are never overwritten")
    with open(path, "w") as f:
        json.dump(frozen, f, indent=1, default=str)
    return path


def log_entries(path: str | None = None) -> list[dict]:
    path = path or LOG
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def run(args) -> dict:
    with open(args.spec_file) as f:
        frozen = json.load(f)
    spec = frozen["spec"]
    log_path = getattr(args, "log", None) or LOG
    if digest(frozen) != frozen.get("digest"):
        raise SystemExit("the frozen file was edited after freezing (digest mismatch)")
    if frozen["code"] != code_hash():
        raise SystemExit("the harness, engine or data code changed since this spec was frozen; refreezing is a new, counted decision")
    dev = Lab(args.data.split(","), ledger_dir=getattr(args, "ledger_dir", None) or LEDGER_DIR)
    if spec["data"] != dev.fp or frozen["dev_fp"] != dev.fp:
        raise SystemExit("the development recording differs from the one this spec was frozen on")
    for name, h in spec["set_hashes"].items():
        if SOURCE_HASH.get(name) != h:
            raise SystemExit(f"feature set '{name}' changed (or is unavailable) since freezing")
    hold_paths = [h for h in args.holdout.split(",") if h]
    scratch = tempfile.mkdtemp(prefix="holdout_")
    hold = Lab(hold_paths, ledger_dir=os.path.join(scratch, "ledger"), cache_dir=os.path.join(scratch, "cache"), unseal=True)
    if {t.mint for t in dev.tokens} & {t.mint for t in hold.tokens}:
        raise SystemExit("the sealed recording overlaps the development recording")
    if min(t.t_launch for t in hold.tokens) < max(t.t_launch for t in dev.tokens) + EMBARGO_MS:
        raise SystemExit("the sealed recording does not start after the development recording (plus the embargo)")
    sid = frozen["spec_id"]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        lf.seek(0)
        done = [json.loads(line) for line in lf if line.strip()]
        for e in done:
            if e["spec_id"] == sid:
                print("this spec was already confirmed; its recorded result:")
                return e
        if len({e["spec_id"] for e in done}) >= MAX_SPECS:
            raise SystemExit(f"the sealed recording has already been used for {MAX_SPECS} specs")
        feats, delay, policy, cost, q = spec["features"], spec["delay_s"], spec["policy"], spec["cost"], spec["q"]
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
        p = float((1 + (boot <= 0).sum()) / (len(boot) + 1))
        n_sel = int(selected.sum())
        alpha = 0.05 / MAX_SPECS
        result = {"name": frozen["name"], "spec_id": sid, "holdout_launches": hold.n, "holdout_tradeable": int(ok_h.sum()), "n_selected": n_sel,
                  "mean_per_trade": float(np.mean(yh[selected])) if n_sel else None, "ci_lo": lo, "ci_hi": hi, "p_mean_le_zero": p,
                  "blind_per_trade": float(np.mean(yh[ok_h])), "win_rate": float(np.mean(yh[selected] > 0)) if n_sel else None,
                  "bonferroni_alpha": alpha, "min_trades": MIN_TRADES, "passes": bool(n_sel >= MIN_TRADES and p < alpha and np.mean(yh[selected]) > 0),
                  "dev_fp": dev.fp, "holdout_fp": hold.fp, "code": code_hash(), "dev_trials_when_frozen": frozen["dev_trials"], "time": time.time()}
        lf.seek(0, 2)
        lf.write(json.dumps(result, default=lambda o: None if o != o else float(o)) + "\n")
        lf.flush()
        os.fsync(lf.fileno())
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="research.confirm")
    p.add_argument("cmd", choices=["freeze", "run"])
    p.add_argument("spec_file", nargs="?")
    p.add_argument("--data", default=",".join(DEV_DATA))
    p.add_argument("--holdout", default="data/raw_run2.jsonl,data/raw_run2b.jsonl")
    p.add_argument("--name"), p.add_argument("--features", default=""), p.add_argument("--model", default="lgbm_reg"), p.add_argument("--policy", default="tp30sl15")
    p.add_argument("--delay", type=float, default=0.0), p.add_argument("--cost", type=float, default=0.001), p.add_argument("--q", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=0), p.add_argument("--note", default="")
    a = p.parse_args(argv)
    print("frozen:", freeze(a)) if a.cmd == "freeze" else print(json.dumps(run(a), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
