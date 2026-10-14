"""Built-in feature sets. Everything here may use only the Context (the world as of the decision time)."""

from __future__ import annotations

import math

from .context import GRADUATION_V_TOKENS, LAUNCH_PRICE, Context, safe_log
from .registry import feature_set

NAN = float("nan")


@feature_set("launch")
def launch(c: Context) -> dict[str, float]:
    sym, name = c.symbol or "", c.name or ""
    return {
        "dev_pct": c.dev_pct, "dev_sol": c.dev_sol, "name_len": len(name), "symbol_len": len(sym),
        "symbol_upper_frac": sum(ch.isupper() for ch in sym) / len(sym) if sym else 0.0, "symbol_digits": sum(ch.isdigit() for ch in sym),
        "name_nonascii": float(any(ord(ch) > 127 for ch in name + sym)), "symbol_in_name": float(bool(sym) and sym.lower() in name.lower()),
        "name_words": len(name.split()),
    }


@feature_set("tape")
def tape(c: Context) -> dict[str, float]:
    prices = [o[1] / o[2] for o in c.obs]
    last_t, first_t = c.obs[-1][0], c.obs[0][0]
    moves = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    v_sol, v_tokens = c.obs[-1][1], c.obs[-1][2]
    return {
        "n_obs": len(c.obs), "first_quote_delay_s": (first_t - c.t_launch) / 1000, "age_s": (c.t0 - c.t_launch) / 1000,
        "log_gain_since_launch": safe_log(c.gain_since_launch), "max_log_gain": safe_log(max(prices) / c.price_launch), "min_log_gain": safe_log(min(prices) / c.price_launch),
        "ups": sum(m > 0 for m in moves), "downs": sum(m < 0 for m in moves), "last_move": moves[-1] if moves else 0.0,
        "since_last_obs_s": (c.t0 - last_t) / 1000, "raised_sol": v_sol - 30.0, "progress": 1 - (v_tokens - GRADUATION_V_TOKENS) / (1_073_000_000 - GRADUATION_V_TOKENS),
        "dev_adjusted_gain": safe_log(c.gain_since_launch) - safe_log(c.v_sol0 / 30.0) if c.v_sol0 > 0 else NAN,
    }


@feature_set("scan")
def scan(c: Context) -> dict[str, float]:
    if c.check is None:
        return {"has_check": 0.0, "rt_ratio": NAN}
    return {"has_check": 1.0, "rt_ratio": c.check[2] / c.check[1] if c.check[1] else NAN}


@feature_set("market")
def market(c: Context) -> dict[str, float]:
    recent = [s for s in c.market if s.t_launch >= c.t0 - 600_000]
    last60 = [s for s in recent if s.t_launch >= c.t0 - 60_000]
    last300 = [s for s in recent if s.t_launch >= c.t0 - 300_000]
    window = list(c.market[-30:])

    def up(s) -> bool:
        return bool(s.obs) and s.last_price is not None and s.last_price / (LAUNCH_PRICE) > 1.1

    quoted = [s for s in window if s.obs]
    return {
        "launches_60s": len(last60), "launches_300s": len(last300), "launches_600s": len(recent),
        "hot_frac_30": sum(up(s) for s in window) / len(window) if window else NAN,
        "mean_dev_30": sum(s.dev_pct for s in window) / len(window) if window else NAN,
        "mean_first_quote_s_30": sum((s.obs[0][0] - s.t_launch) / 1000 for s in quoted) / len(quoted) if quoted else NAN,
        "creators_100": len({s.creator for s in c.market[-100:]}),
    }


@feature_set("creator")
def creator(c: Context) -> dict[str, float]:
    mine = [s for s in c.market if s.creator == c.creator and s.creator]
    pumped = [s for s in mine if s.obs and s.last_price is not None and s.last_price / LAUNCH_PRICE > 1.3]
    return {"creator_earlier": len(mine), "creator_last_10min": sum(s.t_launch >= c.t0 - 600_000 for s in mine), "creator_pumped_before": len(pumped),
            "creator_pumped_frac": len(pumped) / len(mine) if mine else NAN}


DEFAULT_SETS = ("launch", "tape", "scan", "market", "creator")
