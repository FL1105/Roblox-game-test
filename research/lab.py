"""The lab: build features, run a walk-forward trial, record it. One call = one trial in the ledger."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time

import numpy as np

from . import models, stats
from .data import Market, Token, fingerprint, load
from .ledger import Ledger, spec_id
from .outcomes import LATENCY_MS, POLICIES, OutcomeCache, atomic_save_npy, decision_time
from .paths import CACHE_DIR, DEV_DATA, LEDGER_DIR, ROOT
from .registry import FEATURE_SETS, PLUGIN_DIR, PLUGIN_ERRORS, SOURCE_HASH, load_plugins
from .versioning import code_hash

K_BLOCKS = 6            # time-ordered blocks; the first two only ever train
MIN_TRAIN = 60          # fewest tradeable training launches a fold needs
EMBARGO_PAD_MS = 5_000
MIN_TRADES = 60         # a trial needs at least this many trades to be called a finding
MIN_COST = 0.001        # ... and must have been run with a transaction cost (SOL per side): a result that only works for free is not a result
WORKER_TIMEOUT_S = 1800


class DataChanged(RuntimeError):
    pass


class Lab:
    def __init__(self, paths: list[str] | None = None, ledger_dir: str = LEDGER_DIR, cache_dir: str = CACHE_DIR, unseal: bool = False,
                 plugins_dir: str = PLUGIN_DIR):
        from . import features  # noqa: F401  (registers the built-in sets)
        load_plugins(plugins_dir)
        self.paths = [str(p) for p in (paths or DEV_DATA)]
        self.unseal, self.plugins_dir = unseal, plugins_dir
        self.tokens: list[Token] = load(self.paths, unseal=unseal)
        self.fp = fingerprint(self.tokens)
        self.outcomes = OutcomeCache(self.tokens, self.fp, cache_dir)
        self.ledger = Ledger(ledger_dir)
        self.cache_dir = str(cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        self._features: dict[tuple, tuple] = {}
        self.n = len(self.tokens)

    # ---------- one family of trials per dataset ----------
    def _guard_family(self) -> None:
        """Trials are only comparable on the same data. Starting a new family (new or changed data) must be a conscious, recorded act,
        otherwise dropping one token would reset the trial count."""
        path = os.path.join(self.ledger.dir, "family.json")
        entries = self.ledger.entries()
        if not entries:
            return
        if os.path.exists(path):
            with open(path) as f:
                current = json.load(f)["fp"]
        else:
            current = sorted({e["spec"].get("data") for e in entries})[-1]
        if current == self.fp:
            return
        reason = os.environ.get("RESEARCH_ACK_NEW_DATA", "")
        if not reason:
            raise DataChanged(f"the data changed since the first trial (family {current} -> {self.fp}). Earlier trials stay in the ledger and are counted, "
                              "but a new family must be acknowledged: set RESEARCH_ACK_NEW_DATA='<why>'")
        with open(os.path.join(self.ledger.dir, "resets.jsonl"), "a") as f:
            f.write(json.dumps({"from": current, "to": self.fp, "why": reason, "time": time.time()}) + "\n")
        self._record_family()

    def _record_family(self) -> None:
        path = os.path.join(self.ledger.dir, "family.json")
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"fp": self.fp}, f)
        os.replace(tmp, path)

    # ---------- features (always built by a fresh worker process) ----------
    def _set_hash(self, name: str) -> str:
        if name not in FEATURE_SETS:
            why = PLUGIN_ERRORS and "; ".join(f"{k}: {v}" for k, v in PLUGIN_ERRORS.items())
            raise ValueError(f"unknown feature set '{name}'" + (f" (plugin errors: {why})" if why else ""))
        return SOURCE_HASH[name]

    def matrix(self, name: str, delay_s: float) -> tuple[np.ndarray, list[str]]:
        """(n_tokens x n_features) for one feature set; NaN rows where there is no decision to make."""
        key = (name, self._set_hash(name), code_hash(), float(delay_s))
        if key in self._features:
            return self._features[key]
        path = os.path.join(self.cache_dir, f"feat_{self.fp}_{name}_{key[1]}_{key[2]}_{float(delay_s)}.npz")
        z = None
        if os.path.exists(path):
            try:
                z = np.load(path, allow_pickle=False)
                X, names = z["X"], [str(n) for n in z["names"]]
                if len(X) != self.n or str(z["fp"]) != self.fp:
                    z = None
            except (OSError, ValueError, EOFError, KeyError):
                z = None
        if z is None:
            tmp = f"{path}.{os.getpid()}.build.npz"
            cmd = [sys.executable, "-m", "research.worker", "--data", ",".join(self.paths), "--set", name, "--delay", str(float(delay_s)), "--out", tmp,
                   "--plugins-dir", self.plugins_dir] + (["--unseal"] if self.unseal else [])
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=WORKER_TIMEOUT_S, env=env)
            if r.returncode != 0:
                try:
                    msg = json.loads(r.stderr.strip().splitlines()[-1])["error"]
                except (ValueError, KeyError, IndexError):
                    msg = r.stderr[-600:]
                raise ValueError(msg)
            z = np.load(tmp, allow_pickle=False)
            if str(z["set_hash"]) != key[1] or str(z["code_hash"]) != key[2] or str(z["fp"]) != self.fp:
                raise ValueError("the worker built features from different code or data than this lab is using (a plugin changed while running?)")
            X, names = z["X"], [str(n) for n in z["names"]]
            os.replace(tmp, path)
        self._features[key] = (X, names)
        return X, names

    def design(self, sets: list[str], delay_s: float) -> tuple[np.ndarray, list[str]]:
        mats = [self.matrix(s, delay_s) for s in sets]
        return np.hstack([m[0] for m in mats]), [f"{s}.{n}" for s, m in zip(sets, mats) for n in m[1]]

    # ---------- folds ----------
    def folds(self, policy: str, delay_s: float, cost: float):
        """[(train_idx, test_idx, block)] walk-forward. A training launch is allowed only if its own outcome was fully decided before the test block began."""
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
        if cost < 0:
            raise ValueError("cost must be zero or positive")
        return {"features": sorted(features), "set_hashes": {f: self._set_hash(f) for f in sorted(features)}, "model": model, "delay_s": float(delay_s),
                "policy": policy, "policy_rules": POLICIES[policy], "cost": float(cost), "q": float(q), "seed": int(seed), "data": self.fp, "blocks": K_BLOCKS,
                "code": code_hash()}

    def evaluate(self, features: list[str], model: str = "lgbm_reg", delay_s: float = 0.0, policy: str = "tp30sl15", cost: float = MIN_COST, q: float = 0.1,
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
        self._guard_family()
        agent = os.environ.get("RESEARCH_AGENT", "")
        raw_budget = os.environ.get("RESEARCH_BUDGET", "")
        if raw_budget:
            if not agent:
                raise RuntimeError("RESEARCH_BUDGET needs RESEARCH_AGENT (a budget with no owner would cap nothing)")
            try:
                budget = int(raw_budget)
            except ValueError as e:
                raise RuntimeError("RESEARCH_BUDGET must be a whole number") from e
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
            if len(test) == 0 or len(train) < MIN_TRAIN:
                continue  # too little history to learn from: this fold trades nothing
            scores = fit_predict(X[train], y[train], X[test])
            k = max(1, int(round(q * len(test))))
            chosen = test[np.argsort(-scores, kind="stable")[:k]]
            selected[chosen] = True
            per_launch[chosen] = y[chosen]
        vals = np.where(np.isnan(per_launch), 0.0, per_launch)
        sel_t, v_t = selected[test_universe], vals[test_universe]
        lo, hi = stats.ratio_ci(v_t, sel_t, seed=1)
        lo99, _ = stats.ratio_ci(v_t, sel_t, seed=1, level=0.99)
        tradeable_test = np.array([i for i in test_universe if not np.isnan(y[i])], dtype=int)
        n_sel = int(selected.sum())
        summary = {"n_test_launches": int(len(test_universe)), "n_test_tradeable": int(len(tradeable_test)), "n_selected": n_sel,
                   "mean_per_trade": float(np.mean(y[selected])) if n_sel else float("nan"), "ci_lo": lo, "ci_hi": hi, "ci99_lo": lo99,
                   "win_rate": float(np.mean(y[selected] > 0)) if n_sel else float("nan"), "mean_per_launch": float(v_t.mean()),
                   "baseline_per_trade": float(np.mean(y[tradeable_test])) if len(tradeable_test) else float("nan"),
                   "sigma_blind": float(np.std(y[tradeable_test])) if len(tradeable_test) > 1 else float("nan")}
        entry = self.ledger.add(spec, summary, per_launch[test_universe], sel_t, note, agent=agent)
        self._record_family()
        return entry

    # ---------- the verdict over everything tried ----------
    def verdict(self, min_trades: int = MIN_TRADES) -> dict:
        """Reality Check over every comparable trial in the ledger, plus whether the best one qualifies.
        Trials from other data or older rules are counted (and inflate the p-value) but cannot be stacked with these."""
        every = [e for e in self.ledger.entries() if e["spec"].get("model") not in ("random", "const")]
        if not every:
            return {"trials": 0, "total_trials": 0}
        code = code_hash()
        current = [e for e in every if e["spec"].get("data") == self.fp and e["spec"].get("code") == code and e["spec"].get("blocks") == K_BLOCKS]
        stale = len(every) - len(current)
        out = {"trials": len(current), "total_trials": len(every), "stale_trials": stale}
        if not current:
            out.update(passes_reality_check=False, reality_check=None, note="no trial in the ledger was run on this data with the current rules")
            return out
        vecs = [self.ledger.vectors(e["id"]) for e in current]
        V = np.stack([v[0] for v in vecs], axis=1)
        Sel = np.stack([v[1] for v in vecs], axis=1)
        sigma = np.array([e["summary"].get("sigma_blind", float("nan")) for e in current])
        sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, np.nanmedian(sigma[np.isfinite(sigma)]) if np.isfinite(sigma).any() else 1.0)
        rc = stats.reality_check(V, Sel, sigma=sigma, min_trades=min_trades)
        rc["p_inflated"] = min(1.0, rc["p"] * (1 + stale / len(current))) if rc.get("p") is not None else None
        champion = current[rc["best_index"]] if rc.get("best_index") is not None else None
        qualified = [e for e in current if e["summary"]["n_selected"] >= min_trades and e["summary"]["ci99_lo"] > 0]
        out.update(reality_check=rc, champion=champion, qualified=[e["id"] for e in qualified],
                   best=sorted(current, key=lambda e: -(e["summary"]["ci99_lo"] if not math.isnan(e["summary"]["ci99_lo"]) else -1e9))[:5],
                   passes_reality_check=bool(champion is not None and rc["p_inflated"] < 0.05 and champion["id"] in {e["id"] for e in qualified}
                                             and champion["spec"]["cost"] >= MIN_COST))
        return out
