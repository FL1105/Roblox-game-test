"""Small, transparent learned models for hold/sell: logistic regression on the path features.

Two heads, both about the NEXT `HORIZON_S` seconds of holding a position:
    p_down = P(holding loses more than DOWN_PCT)      p_up = P(holding gains more than UP_PCT)
Inference is plain Python on exported JSON so it can run anywhere (including the browser build); training needs numpy.
With a few hundred real tokens this is the honest amount of model: judge it by walk-forward results, not by fit.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .features import FEATURES

HORIZON_S = 20
DOWN_PCT = 2.0
UP_PCT = 3.0

_LOG1P = {"age_s", "liq_sol", "trades_15", "trades_60", "idle_s", "since_peak_s", "dev_pct"}
_SIGNED_LOG1P = {"flow_15", "flow_60"}


def transform(feats: dict[str, float]) -> list[float]:
    """Tame heavy tails before the linear model. Shared by training and inference, so they can never disagree."""
    out = []
    for name in FEATURES:
        x = feats[name]
        if name in _LOG1P:
            x = math.log1p(max(x, 0.0))
        elif name in _SIGNED_LOG1P:
            x = math.copysign(math.log1p(abs(x)), x)
        out.append(x)
    return out


@dataclass(frozen=True)
class Logistic:
    mean: list[float]
    std: list[float]
    coef: list[float]
    intercept: float

    def proba(self, x: list[float]) -> float:
        z = self.intercept + sum(c * (v - m) / s for c, v, m, s in zip(self.coef, x, self.mean, self.std))
        return 1 / (1 + math.exp(-max(min(z, 35.0), -35.0)))

    def to_dict(self) -> dict:
        return {"mean": self.mean, "std": self.std, "coef": self.coef, "intercept": self.intercept}

    @staticmethod
    def from_dict(d: dict) -> "Logistic":
        return Logistic(d["mean"], d["std"], d["coef"], d["intercept"])


@dataclass(frozen=True)
class HoldModel:
    up: Logistic
    down: Logistic
    meta: dict

    def scores(self, feats: dict[str, float]) -> tuple[float, float]:
        x = transform(feats)
        return self.up.proba(x), self.down.proba(x)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump({"features": FEATURES, "up": self.up.to_dict(), "down": self.down.to_dict(), "meta": self.meta}, f, indent=1)

    @staticmethod
    def load(path: str) -> "HoldModel":
        with open(path) as f:
            d = json.load(f)
        if d["features"] != FEATURES:
            raise ValueError("model was trained on a different feature list; retrain it")
        return HoldModel(Logistic.from_dict(d["up"]), Logistic.from_dict(d["down"]), d.get("meta", {}))


@dataclass(frozen=True)
class ModelParams:
    sell_margin: float = 0.10   # sell when p_down - p_up reaches this
    min_age_s: float = 5.0


class ModelExit:
    def __init__(self, model: HoldModel, params: ModelParams = ModelParams()):
        self.model, self.params = model, params

    def decide(self, ret_pct: float, peak_ret_pct: float, f: dict[str, float]) -> str | None:
        if f["age_s"] < self.params.min_age_s:
            return None
        p_up, p_down = self.model.scores(f)
        return "model_sell" if p_down - p_up >= self.params.sell_margin else None


# ---------------- training (numpy only needed from here down) ----------------
def fit_logistic(X, y, l2: float = 1.0, iters: int = 50) -> Logistic:
    """Ridge-regularised logistic regression by Newton steps (IRLS) on standardised inputs."""
    import numpy as np

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    mean, std = X.mean(0), X.std(0)
    std[std < 1e-9] = 1.0
    Z = np.hstack([(X - mean) / std, np.ones((len(X), 1))])
    w = np.zeros(Z.shape[1])
    reg = np.eye(Z.shape[1]) * l2
    reg[-1, -1] = 0.0  # never shrink the intercept
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -35, 35)))
        grad = Z.T @ (p - y) + reg @ w
        hess = (Z * (p * (1 - p))[:, None]).T @ Z + reg + 1e-9 * np.eye(Z.shape[1])
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return Logistic(mean.tolist(), std.tolist(), w[:-1].tolist(), float(w[-1]))


def auc(scores, labels) -> float:
    """Probability a random positive outranks a random negative (0.5 = no skill)."""
    import numpy as np

    s, y = np.asarray(scores, dtype=float), np.asarray(labels, dtype=int)
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order))
    allv = np.concatenate([pos, neg])[order]
    i = 0
    while i < len(allv):  # average ranks over ties
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))
