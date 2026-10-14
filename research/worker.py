"""Builds one feature set's matrix in a FRESH process, in decision-time order, and refuses it unless the set behaves like a pure function of the past.

    python -m research.worker --data data/raw_main.jsonl --set launch --delay 0 --out /tmp/launch.npz

Run in a new interpreter for every (set, delay) so no state can carry from one token, one delay or one trial to the next, and so a plugin can only
ever touch its own process. Checks, all on the real data:
  * pure:        calling the function again (and in reversed and shuffled orders) gives exactly the same numbers
  * scramble:    corrupting everything after the decision time (values) changes nothing
  * truncation:  deleting everything after the decision time (tokens, quotes, checks, end rows) changes nothing, and the whole Context is identical
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import math
import os
import random
import sys

import numpy as np

from . import features as _builtin_sets  # noqa: F401  (registers the built-in feature sets)
from .context import build_context
from .data import Market, Token, fingerprint, load
from .outcomes import decision_time, tradeable
from .registry import FEATURE_SETS, PLUGIN_DIR, PLUGIN_ERRORS, SOURCE_HASH, load_plugins
from .versioning import code_hash


class Violation(ValueError):
    pass


def _eq(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        if x != y:
            return False
    return True


def scramble(tokens: list[Token], t: int, seed: int) -> list[Token]:
    """The world with everything after t replaced by nonsense (values only; counts and times stay)."""
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


def truncate(tokens: list[Token], t: int) -> list[Token]:
    """The world as it was at t: later tokens do not exist, later quotes, checks and end rows are gone."""
    out = []
    for tok in tokens:
        if tok.t_launch > t:
            continue
        c = copy.copy(tok)
        c.events = []
        c.obs = [o for o in tok.obs if o[0] <= t]
        c.check = tok.check if tok.check is not None and tok.check[0] <= t else None
        if tok.end_t is not None and tok.end_t > t:
            c.end_t, c.end_reason = None, None
        out.append(c)
    return out   # a truncated world keeps the original indices (Market.view only uses them to exclude the focal token)


def context_of(world: list[Token], idx: int, delay_s: float):
    tok = next(t for t in world if t.idx == idx)
    return build_context(tok, Market(world), delay_s)


def compute_rows(tokens: list[Token], fn, delay_s: float) -> dict[int, dict]:
    market = Market(tokens)
    order = sorted((t for t in tokens if tradeable(t, delay_s)), key=lambda t: (decision_time(t, delay_s), t.idx))   # decision-time order
    rows = {}
    for tok in order:
        rows[tok.idx] = fn(build_context(tok, market, delay_s))
    return rows


def run_checks(tokens: list[Token], fn, delay_s: float, rows: dict[int, dict], name: str, sample: int = 150, seed: int = 0) -> dict:
    rng = random.Random(seed)
    pool = sorted(rows)
    picked = rng.sample(pool, min(sample, len(pool)))
    market = Market(tokens)
    by_idx = {t.idx: t for t in tokens}
    # pure: again, reversed, shuffled
    for label, order in (("again", picked), ("reversed", picked[::-1]), ("shuffled", rng.sample(picked, len(picked)))):
        for i in order:
            again = fn(build_context(by_idx[i], market, delay_s))
            if not _eq(rows[i], again):
                raise Violation(f"feature set '{name}' is not a pure function of its context: it gave different numbers when called {label} "
                                f"(token {by_idx[i].mint[:8]}); a set that keeps state between calls can carry information from other tokens' futures")
    # scramble and truncation of everything after the decision time
    for i in picked[: max(1, sample // 2)]:
        tok = by_idx[i]
        t0 = decision_time(tok, delay_s)
        real_ctx = build_context(tok, market, delay_s)
        bad = scramble(tokens, t0, seed=i)
        ctx_bad = build_context(next(t for t in bad if t.idx == i), Market(bad), delay_s)
        if not _eq(rows[i], fn(ctx_bad)):
            raise Violation(f"feature set '{name}' uses information from after the decision time (changes when later quotes, checks or launches are scrambled; token {tok.mint[:8]})")
        cut = truncate(tokens, t0)
        ctx_cut = build_context(next(t for t in cut if t.idx == i), Market(cut), delay_s)
        if dataclasses.asdict(real_ctx) != dataclasses.asdict(ctx_cut):
            raise Violation(f"the Context for token {tok.mint[:8]} differs when the future is deleted: the harness leaks the future to features (harness bug)")
        if not _eq(rows[i], fn(ctx_cut)):
            raise Violation(f"feature set '{name}' depends on information that does not exist yet at the decision time (changes when the future is deleted; token {tok.mint[:8]})")
    return {"checked": len(picked)}


def matrix(tokens: list[Token], name: str, delay_s: float, fn=None) -> tuple[np.ndarray, list[str], dict]:
    fn = fn or FEATURE_SETS[name]
    rows = compute_rows(tokens, fn, delay_s)
    names = None
    for r in rows.values():
        if names is None:
            names = sorted(r)
        elif sorted(r) != names:
            raise Violation(f"feature set '{name}' returned different feature names for different tokens")
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or math.isinf(v) for v in r.values()):
            raise Violation(f"feature set '{name}' must return finite numbers (or NaN)")
    names = names or []
    info = run_checks(tokens, fn, delay_s, rows, name)
    X = np.full((len(tokens), len(names)), np.nan)
    for i, r in rows.items():
        X[i] = [float(r[k]) for k in names]
    return X, names, info


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="research.worker")
    p.add_argument("--data", required=True)
    p.add_argument("--set", required=True)
    p.add_argument("--delay", type=float, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--plugins-dir", default=PLUGIN_DIR)
    p.add_argument("--unseal", action="store_true")
    a = p.parse_args(argv)
    from . import features  # noqa: F401
    load_plugins(a.plugins_dir)
    if a.set not in FEATURE_SETS:
        reason = "; ".join(f"{k}: {v}" for k, v in PLUGIN_ERRORS.items()) or "no such set"
        print(json.dumps({"error": f"feature set '{a.set}' is not available ({reason})"}), file=sys.stderr)
        return 2
    tokens = load(a.data.split(","), unseal=a.unseal)
    try:
        X, names, info = matrix(tokens, a.set, a.delay)
    except Violation as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 3
    tmp = f"{a.out}.{os.getpid()}.tmp.npz"
    np.savez(tmp, X=X, names=np.array(names), set_hash=SOURCE_HASH[a.set], code_hash=code_hash(), fp=fingerprint(tokens), checked=info["checked"])
    os.replace(tmp, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
