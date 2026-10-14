"""Train the hold/sell model on real recordings and say honestly how well it works on launches it never saw.

    python -m sniper.policy.train raw1.jsonl [raw2.jsonl ...] --out model.json [--test-frac 0.3]

Launches are ordered by time: the earlier ones train, the later ones are the untouched test set.
"""

from __future__ import annotations

import argparse
import sys
import time

from ..live.convert import read_raw, token_paths
from .dataset import token_samples
from .features import FEATURES
from .model import DOWN_PCT, HORIZON_S, UP_PCT, HoldModel, auc, fit_logistic
from .model import transform  # noqa: F401  (documented dependency of the sample format)

L2_CHOICES = (0.3, 3.0, 30.0, 300.0)
MIN_TRAIN_LAUNCHES = 30
MIN_TEST_LAUNCHES = 10


def split_tokens(paths: dict[str, dict], test_frac: float) -> tuple[list[dict], list[dict]]:
    ordered = sorted(paths.values(), key=lambda p: p["create"]["t"])
    cut = int(len(ordered) * (1 - test_frac))
    return ordered[:cut], ordered[cut:]


def build(paths: list[dict]):
    import numpy as np

    X, up, down = [], [], []
    for p in paths:
        for x, u, d, _ in token_samples(p):
            X.append(x); up.append(u); down.append(d)
    return np.array(X, dtype=float), np.array(up), np.array(down)


def choose_l2(X, y) -> float:
    """Pick the ridge strength on the last fifth of the training samples (still time-ordered, never the test set)."""
    cut = int(len(X) * 0.8)
    best, best_auc = L2_CHOICES[1], -1.0
    if y[:cut].min() == y[:cut].max() or len(set(y[cut:])) < 2:
        return best
    for l2 in L2_CHOICES:
        a = auc([m for m in map(fit_logistic(X[:cut], y[:cut], l2).proba, X[cut:])], y[cut:])
        if a > best_auc:
            best, best_auc = l2, a
    return best


def single_feature_aucs(X, y) -> dict[str, float]:
    """How well does each raw feature alone rank the labels? The learned model has to beat the best of these to matter."""
    out = {}
    for i, name in enumerate(FEATURES):
        a = auc(X[:, i], y)
        out[name] = a if a == a else 0.5
    return out


def train(paths: dict[str, dict], test_frac: float = 0.3) -> tuple[HoldModel, str]:
    train_p, test_p = split_tokens(paths, test_frac)
    Xtr, up_tr, down_tr = build(train_p)
    Xte, up_te, down_te = build(test_p)
    # Samples within one launch are strongly correlated, so what limits trust is the number of LAUNCHES, not samples.
    if len(train_p) < MIN_TRAIN_LAUNCHES or len(test_p) < MIN_TEST_LAUNCHES or len(Xtr) < 50 or len(Xte) < 20:
        raise SystemExit(f"not enough launches to train honestly ({len(train_p)} train / {len(test_p)} test; need {MIN_TRAIN_LAUNCHES} / {MIN_TEST_LAUNCHES}); record more data")
    lines = [f"{len(train_p)} training launches ({len(Xtr)} samples), {len(test_p)} test launches ({len(Xte)} samples)",
             f"label: over the next {HORIZON_S}s holding gains >{UP_PCT:g}% (up) or loses >{DOWN_PCT:g}% (down). "
             f"base rates train: up {up_tr.mean() * 100:.1f}%, down {down_tr.mean() * 100:.1f}%; test: up {up_te.mean() * 100:.1f}%, down {down_te.mean() * 100:.1f}%"]
    heads, aucs = {}, {}
    for name, ytr, yte in (("up", up_tr, up_te), ("down", down_tr, down_te)):
        l2 = choose_l2(Xtr, ytr)
        m = fit_logistic(Xtr, ytr, l2)
        heads[name] = m
        a_test = auc([m.proba(list(r)) for r in Xte], yte)
        singles = single_feature_aucs(Xte, yte)
        best_f, best_a = max(singles.items(), key=lambda kv: abs(kv[1] - 0.5))
        aucs[name] = a_test
        lines.append(f"  {name:5} head: ridge {l2:g}  test AUC {a_test:.3f}   | best single feature on test: {best_f} AUC {best_a:.3f}  (0.5 = no skill)")
    meta = {"trained_at": time.strftime("%Y-%m-%d %H:%M:%S"), "train_launches": len(train_p), "test_launches": len(test_p), "test_auc_up": aucs["up"], "test_auc_down": aucs["down"],
            "horizon_s": HORIZON_S, "up_pct": UP_PCT, "down_pct": DOWN_PCT}
    return HoldModel(heads["up"], heads["down"], meta), "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sniper.policy.train")
    p.add_argument("raw", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--test-frac", type=float, default=0.3)
    a = p.parse_args(argv)
    paths = token_paths([r for path in a.raw for r in read_raw(path)])
    model, report = train(paths, a.test_frac)
    print(report)
    model.save(a.out)
    print(f"saved {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
