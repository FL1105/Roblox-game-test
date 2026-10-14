"""The lab: build features, run a walk-forward trial, record it. One call = one trial in the ledger."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random

import numpy as np

from . import models, stats
from .context import build_context
from .data import Market, Token, fingerprint, load
from .ledger import Ledger, spec_id
from .outcomes import LATENCY_MS, POLICIES, OutcomeCache, decision_time, tradeable
from .registry import FEATURE_SETS, SOURCE_FILES, file_hash, load_plugins

K_BLOCKS = 6            # time-ordered blocks; the first two only ever train
MIN_TRAIN = 60          # fewest tradeable training launches a fold needs
EMBARGO_PAD_MS = 5_000


def _same(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        if x != y:
            return False
    return True


def corrupt_future(tokens: list[Token], t: int, seed: int = 0) -> list[Token]:
    """A copy of the world in which everything after time t has been replaced by nonsense: observations rewritten, the sell-route check
    changed, and tokens launched later given random names, creators and dev buys. Features that really use only the past cannot tell."""
    rng = random.Random(seed)
    out = []
    for tok in tokens:
        c = copy.copy(tok)
        c.events = []
        c.obs = [(o[0], o[1] * rng.uniform(0.2, 5), o[2] * rng.uniform(0.2, 5)) if o[0] > t else o for o in tok.obs]
        if c.check is not None and c.check[0] > t:
            c.check = (c.check[0], c.check[1], c.check[2] * rng.uniform(0, 1))
        if tok.end_t is not None and tok.end_t > t:
            c.end_t, c.end_reason = tok.end_t + rng.randint(1, 10_000), rng.choice(["idle", "window_over", None])
        if tok.t_launch > t:
            c.creator, c.name, c.symbol = f"X{rng.random()}", f"junk{rng.random()}", "J" * rng.randint(1, 9)
            c.dev_tokens, c.dev_sol = rng.uniform(0, 5e8), rng.uniform(0, 30)
        out.append(c)
    return out


class Lab:
    def __init__(self, paths: list[str] | None = None, ledger_dir: str = "research/ledger", cache_dir: str = "research/cache", tokens: list[Token] | None = None):
        load_plugins()
        from . import features  # noqa: F401  (registers the built-in sets)
        self.tokens = tokens if tokens is not None else load(paths or ["data/raw_main.jsonl"])
        self.fp = fingerprint(self.tokens)
        self.market = Market(self.tokens)
        self.outcomes = OutcomeCache(self.tokens, self.fp, os.path.join(cache_dir))
        self.ledger = Ledger(ledger_dir)
        self.cache_dir = cache_dir
        self._features: dict[tuple, np.ndarray] = {}
        self._checked: set[tuple] = set()
        self.n = len(self.tokens)

    # ---------- features ----------
    def _set_hash(self, name: str) -> str:
        return file_hash(SOURCE_FILES[name])

    def check_no_lookahead(self, name: str, delay_s: float, sample: int = 40, seed: int = 0) -> None:
        """Demand identical features when everything after the decision time is corrupted. Raises if the set peeks."""
        key = (name, self._set_hash(name), float(delay_s))
        if key in self._checked:
            return
        rng = random.Random(seed)
        pool = [t for t in self.tokens if tradeable(t, delay_s)]
        for tok in rng.sample(pool, min(sample, len(pool))):
            t0 = decision_time(tok, delay_s)
            real = build_context(tok, self.market, delay_s)
            bad_tokens = corrupt_future(self.tokens, t0, seed=tok.idx)
            fake = build_context(bad_tokens[tok.idx], Market(bad_tokens), delay_s)
            a, b = FEATURE_SETS[name](real), FEATURE_SETS[name](fake)
            if not _same(a, b):
                diff = [k for k in a if not (a[k] == b.get(k) or (isinstance(a[k], float) and math.isnan(a[k]) and isinstance(b.get(k), float) and math.isnan(b[k])))]
                raise ValueError(f"feature set '{name}' uses information from after the decision time (differs for {diff[:5]} when the future is corrupted; token {tok.mint[:8]})")
        self._checked.add(key)

    def matrix(self, name: str, delay_s: float) -> tuple[np.ndarray, list[str]]:
        """(n_tokens x n_features) for one feature set; NaN rows where there is no decision to make."""
        key = (name, self._set_hash(name), float(delay_s))
        path = os.path.join(self.cache_dir, f"feat_{self.fp}_{name}_{key[1]}_{float(delay_s)}.npz")
        if key not in self._features and os.path.exists(path):
            z = np.load(path, allow_pickle=False)
            self._features[key] = (z["X"], list(z["names"]))
        if key not in self._features:
            self.check_no_lookahead(name, delay_s)
            rows, names = [], None
            for tok in self.tokens:
                ctx = build_context(tok, self.market, delay_s)
                if ctx is None:
                    rows.append(None)
                    continue
                f = FEATURE_SETS[name](ctx)
                if names is None:
                    names = sorted(f)
                elif sorted(f) != names:
                    raise ValueError(f"feature set '{name}' returned different feature names for different tokens")
                rows.append([float(f[k]) for k in names])
            names = names or []
            X = np.full((self.n, len(names)), np.nan)
            for i, r in enumerate(rows):
                if r is not None:
                    X[i] = r
            os.makedirs(self.cache_dir, exist_ok=True)
            np.savez(path, X=X, names=np.array(names))
            self._features[key] = (X, names)
        self._checked.add(key)
        return self._features[key]

    def design(self, sets: list[str], delay_s: float) -> tuple[np.ndarray, list[str]]:
        mats = [self.matrix(s, delay_s) for s in sets]
        return np.hstack([m[0] for m in mats]), [f"{s}.{n}" for s, m in zip(sets, mats) for n in m[1]]

    # ---------- folds ----------
    def folds(self, policy: str, delay_s: float, cost: float):
        """[(train_idx, test_idx)] walk-forward. A training launch is allowed only if its own outcome was fully decided before the test block began."""
        y = self.outcomes.returns(policy, delay_s, cost)
        ok = ~np.isnan(y)
        hold = POLICIES[policy].get("max_hold_s", 600)
        out_end = np.array([(decision_time(t, delay_s) or 0) + LATENCY_MS + hold * 1000 + EMBARGO_PAD_MS for t in self.tokens])
        launch = np.array([t.t_launch for t in self.tokens])
        block = np.arange(self.n) * K_BLOCKS // self.n
        folds = []
        for j in range(2, K_BLOCKS):
            start = launch[block == j].min()
            train = np.where(ok & (out_end <= start) & (block < j))[0]
            test = np.where(ok & (block == j))[0]
            folds.append((train, test, j))
        return folds, block

    # ---------- a trial ----------
    def spec(self, features: list[str], model: str, delay_s: float, policy: str, cost: float, q: float, seed: int) -> dict:
        for f in features:
            if f not in FEATURE_SETS:
                raise ValueError(f"unknown feature set '{f}'")
        return {"features": sorted(features), "set_hashes": {f: self._set_hash(f) for f in sorted(features)}, "model": model, "delay_s": float(delay_s),
                "policy": policy, "cost": float(cost), "q": float(q), "seed": int(seed), "data": self.fp, "blocks": K_BLOCKS}

    def evaluate(self, features: list[str], model: str = "lgbm_reg", delay_s: float = 0.0, policy: str = "tp30sl15", cost: float = 0.0, q: float = 0.1,
                 seed: int = 0, note: str = "") -> dict:
        """Run one walk-forward trial and record it. Returns the ledger entry (with `summary`)."""
        if policy not in POLICIES:
            raise ValueError(f"unknown policy '{policy}'")
        if model not in models.MODELS:
            raise ValueError(f"unknown model '{model}'")
        if not 0 < q <= 1:
            raise ValueError("q must be in (0, 1]")
        spec = self.spec(features, model, delay_s, policy, cost, q, seed)
        existing = self.ledger.get(spec_id(spec))
        if existing:
            return existing
        agent = os.environ.get("RESEARCH_AGENT", "")
        budget = int(os.environ.get("RESEARCH_BUDGET", "0") or 0)
        if agent and budget:
            used = sum(1 for e in self.ledger.entries() if e.get("agent") == agent)
            if used >= budget:
                raise RuntimeError(f"trial budget exhausted for '{agent}' ({used}/{budget}): choose your remaining trials carefully")
        X, _ = self.design(sorted(features), delay_s) if features else (np.zeros((self.n, 0)), [])
        y = self.outcomes.returns(policy, delay_s, cost)
        folds, block = self.folds(policy, delay_s, cost)
        fit_predict = models.make(model, seed)
        per_launch = np.full(self.n, np.nan)
        selected = np.zeros(self.n, dtype=bool)
        test_universe = np.where(block >= 2)[0]
        per_launch[test_universe] = 0.0
        for train, test, _j in folds:
            if len(test) == 0:
                continue
            if len(train) < MIN_TRAIN:
                continue  # too little history to learn from: this fold trades nothing
            scores = fit_predict(X[train], y[train], X[test])
            k = max(1, int(round(q * len(test))))
            order = np.argsort(-scores, kind="stable")[:k]
            chosen = test[order]
            selected[chosen] = True
            per_launch[chosen] = y[chosen]
        in_test = ~np.isnan(per_launch)
        vals = np.where(in_test, per_launch, 0.0)
        sel_t = selected[test_universe]
        v_t = vals[test_universe]
        lo, hi = stats.ratio_ci(v_t, sel_t, seed=1)
        # the blind baseline over the same test launches, same policy/delay/cost: what buying everything would have returned
        tradeable_test = np.array([i for i in test_universe if not np.isnan(y[i])], dtype=int)
        baseline = float(np.mean(y[tradeable_test])) if len(tradeable_test) else float("nan")
        n_sel = int(selected.sum())
        summary = {"n_test_launches": int(len(test_universe)), "n_test_tradeable": int(len(tradeable_test)), "n_selected": n_sel,
                   "mean_per_trade": float(np.mean(y[selected])) if n_sel else float("nan"), "ci_lo": lo, "ci_hi": hi,
                   "win_rate": float(np.mean(y[selected] > 0)) if n_sel else float("nan"), "mean_per_launch": float(v_t.mean()), "baseline_per_trade": baseline}
        return self.ledger.add(spec, summary, per_launch[test_universe], sel_t, note, agent=agent)

    # ---------- the verdict over everything tried ----------
    def verdict(self, min_trades: int = 30) -> dict:
        """Reality Check over every trial in the ledger that shares this data, plus the best candidates and whether they survive."""
        entries = [e for e in self.ledger.entries() if e["spec"].get("data") == self.fp and e["spec"].get("model") not in ("random", "const")]
        if not entries:
            return {"trials": 0}
        vecs = [self.ledger.vectors(e["id"]) for e in entries]
        V = np.stack([v[0] for v in vecs], axis=1)
        Sel = np.stack([v[1] for v in vecs], axis=1)
        rc = stats.reality_check(V, Sel)
        ranked = sorted(entries, key=lambda e: -(e["summary"]["ci_lo"] if not math.isnan(e["summary"]["ci_lo"]) else -1e9))
        survivors = [e for e in ranked if e["summary"]["n_selected"] >= min_trades and e["summary"]["ci_lo"] > 0]
        return {"trials": len(entries), "reality_check": rc, "best": ranked[:5], "ci_positive_unadjusted": [e["id"] for e in survivors],
                "passes_reality_check": bool(rc["p"] < 0.05 and survivors)}
