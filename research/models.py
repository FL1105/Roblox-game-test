"""The scorers. Each takes a feature matrix and a return vector and gives a score: higher = buy first."""

from __future__ import annotations

import numpy as np

CLIP = (-40.0, 100.0)   # returns are clipped for fitting only (a +780% fluke would otherwise be the whole model); evaluation uses the raw returns


def make(name: str, seed: int):
    """Returns fit_predict(X_train, y_train, X_test) -> scores."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if name == "random":
        rng = np.random.default_rng(seed)
        return lambda Xtr, ytr, Xte: rng.random(len(Xte))
    if name == "const":
        return lambda Xtr, ytr, Xte: np.zeros(len(Xte))
    if name == "ridge":
        def fit_predict(Xtr, ytr, Xte):
            m = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), Ridge(alpha=300.0))
            m.fit(Xtr, np.clip(ytr, *CLIP))
            return m.predict(Xte)
        return fit_predict
    if name == "logit":
        def fit_predict(Xtr, ytr, Xte):
            y = (ytr > 0).astype(int)
            if y.min() == y.max():
                return np.zeros(len(Xte))
            m = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), LogisticRegression(C=0.05, max_iter=500))
            m.fit(Xtr, y)
            return m.predict_proba(Xte)[:, 1]
        return fit_predict
    if name in ("lgbm_reg", "lgbm_clf"):
        import lightgbm as lgb
        params = dict(n_estimators=150, learning_rate=0.03, num_leaves=6, min_child_samples=25, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                      reg_lambda=10.0, random_state=seed, n_jobs=1, verbose=-1, deterministic=True, force_row_wise=True)

        def fit_predict(Xtr, ytr, Xte):
            if name == "lgbm_reg":
                m = lgb.LGBMRegressor(**params)
                m.fit(Xtr, np.clip(ytr, *CLIP))
                return m.predict(Xte)
            y = (ytr > 0).astype(int)
            if y.min() == y.max():
                return np.zeros(len(Xte))
            m = lgb.LGBMClassifier(**params)
            m.fit(Xtr, y)
            return m.predict_proba(Xte)[:, 1]
        return fit_predict
    raise ValueError(f"unknown model '{name}'")


MODELS = ("ridge", "logit", "lgbm_reg", "lgbm_clf", "random", "const")
