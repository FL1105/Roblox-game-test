"""Block-bootstrap confidence intervals and White's Reality Check across every trial ever run."""

from __future__ import annotations

import numpy as np

BLOCK = 25          # consecutive launches resampled together (launches close in time are not independent)
DRAWS = 4000


def chunk_ids(n: int, block: int | None = None) -> np.ndarray:
    return np.arange(n) // (block or BLOCK)


def ratio_ci(values: np.ndarray, selected: np.ndarray, seed: int = 0, draws: int = DRAWS, level: float = 0.95) -> tuple[float, float]:
    """CI of the mean return per selected trade, resampling blocks of consecutive launches. `values` is 0 where not selected."""
    n = len(values)
    if selected.sum() == 0:
        return float("nan"), float("nan")
    ids = chunk_ids(n)
    nb = ids.max() + 1
    s = np.bincount(ids, weights=values, minlength=nb)
    c = np.bincount(ids, weights=selected.astype(float), minlength=nb)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nb, size=(draws, nb))
    num, den = s[idx].sum(1), c[idx].sum(1)
    ok = den > 0
    means = num[ok] / den[ok]
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def reality_check(per_launch: np.ndarray, selected: np.ndarray | None = None, seed: int = 0, draws: int = DRAWS, sigma: np.ndarray | None = None,
                  min_trades: int = 30) -> dict:
    """White's Reality Check. `per_launch` is (launches x trials): each trial's return on each launch (0 where it did not trade);
    `selected` marks where it traded (default: wherever the return is non-zero).

    Each trial is scored by (mean return per trade) x sqrt(number of trades) / sigma, a z-like score that rewards a bigger sample but needs no noisy
    variance estimate: `sigma` is a dispersion that does not depend on what the trial selected (the spread of the blind returns for its policy, delay
    and cost), so a high-variance policy cannot raise the bar for every other trial. Trials with fewer than `min_trades` trades are ignored.
    The question asked: is the best trial's score larger than the best of this many zero-edge trials would reach by luck? The null is built by
    resampling blocks of launches and re-centring every trial on its own observed mean."""
    n, k = per_launch.shape
    if k == 0:
        return {"p": float("nan"), "trials": 0}
    if selected is None:
        selected = per_launch != 0
    if sigma is None:
        sigma = np.ones(k)
    ids = chunk_ids(n)
    nb = ids.max() + 1
    S = np.stack([np.bincount(ids, weights=per_launch[:, j], minlength=nb) for j in range(k)], axis=1)
    C = np.stack([np.bincount(ids, weights=selected[:, j].astype(float), minlength=nb) for j in range(k)], axis=1)
    count = C.sum(0)
    live = count >= min_trades
    if not live.any():
        return {"p": 1.0, "trials": int(k), "live_trials": 0, "best_t": 0.0, "best_index": None}
    S, C, count, sig = S[:, live], C[:, live], count[live], np.asarray(sigma, dtype=float)[live]
    mean = S.sum(0) / count
    scale = np.sqrt(count) / sig
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nb, size=(draws, nb))
    t_null = np.empty(draws)
    for b in range(draws):
        sel = idx[b]
        cb = C[sel].sum(0)
        mb = np.where(cb > 0, S[sel].sum(0) / np.where(cb > 0, cb, 1), mean)
        t_null[b] = np.max((mb - mean) * scale)
    t_all = mean * scale
    t_obs = float(t_all.max())
    return {"p": float((1 + (t_null >= t_obs).sum()) / (draws + 1)), "trials": int(k), "live_trials": int(live.sum()), "best_t": t_obs,
            "best_index": int(np.flatnonzero(live)[int(np.argmax(t_all))])}
