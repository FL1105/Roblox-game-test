"""The browser build (standalone.js) must behave exactly like the Python code it was ported from."""

import dataclasses
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sniper.policy.features import FEATURES
from sniper.policy.model import HoldModel, Logistic, ModelExit
from sniper.pump import synth
from sniper.paper.engine import PaperEngine
from sniper.paper.tuner import DEFAULT_PARAMS, Tuner, TunerConfig, alpha_for, blend_changes, clone_passes, mulberry32, paired_gate, parse_tuner, resample_counts, snap
from sniper.pump.events import to_wire
from sniper.terminal.state import DEFAULT_ROWS, PRESETS, TerminalState, default_strategy, parse_settings, settings_dict

ROOT = Path(__file__).resolve().parent.parent
WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


def fixed_model():
    """Deterministic weights, so Python and JavaScript evaluate the very same model."""
    import random
    rng = random.Random(42)
    n = len(FEATURES)

    def head(bias):
        return Logistic([rng.uniform(-1, 1) for _ in range(n)], [rng.uniform(0.5, 2.0) for _ in range(n)], [rng.uniform(-0.8, 0.8) for _ in range(n)], bias)

    return HoldModel(head(-1.0), head(-0.5), {})


def model_json(m):
    return {"features": FEATURES, "up": m.up.to_dict(), "down": m.down.to_dict()}


def feature_cases(n=220, seed=5):
    """Random token paths cut at random moments: path features and model scores computed by Python, to be recomputed by JavaScript."""
    import random
    from sniper.policy.features import path_features, tape_from_launch
    from sniper.pump.events import NewToken, Trade

    rng = random.Random(seed)
    model = fixed_model()
    events = [e for e in synth.generate(21, 120, 1_700_000_000_000) if isinstance(e, (NewToken, Trade))]
    by_mint = {}
    for e in events:
        by_mint.setdefault(e.mint, []).append(e)
    cases = []
    for mint, evs in by_mint.items():
        launch, trades = evs[0], [e for e in evs[1:] if isinstance(e, Trade)]
        for _ in range(2):
            k = rng.randint(0, len(trades))
            now = (trades[k - 1].t if k else launch.t) + rng.choice([0, 0, 1_000, 7_000, 45_000])
            tape = tape_from_launch(launch)
            for tr in trades[:k]:
                tape.add(tr.t, tr.v_sol, tr.v_tokens)
            feats = path_features(tape, now)
            up, down = model.scores(feats)
            cases.append({"launch": wire(launch), "trades": [wire(t) for t in trades[:k]], "now": now, "features": feats, "scores": [up, down]})
    rng.shuffle(cases)
    return cases[:n]


wire = to_wire


def settings_cases():
    good = [
        {}, {"take_profit": 45}, {"amount_min": 0.02, "amount_max": 0.3}, {"verdicts": ["PASS"]}, {"verdicts": ["AVOID", "PASS", "PASS"]},
        {"trailing_stop_pct": 12, "slippage_pct": 7, "min_liquidity_sol": 3, "size_mode": "random"},
        {"auto_compound": False, "use_blacklist": False, "honeypot_detection": False, "exit_on_creator_sell": False},
        {"blacklist": [WALLET, WALLET]}, {"max_open": 7.9, "latency_ms": 1234.5}, {"fee_bps": 0, "tx_cost_sol": 0.1},
        {"stop_loss": 99, "take_profit": 1000, "max_hold_s": 3600}, {"max_entry_mcap_sol": 1, "max_dev_buy_pct": 0},
        {"exit_mode": "smart"}, {"entry_mode": "confirm", "confirm_gain_pct": 25, "confirm_min_flow_sol": 0.4, "confirm_window_s": 90},
        {"confirm_gain_pct": 0, "confirm_window_s": 5, "confirm_min_flow_sol": 100},
        {"min_trades": 3, "max_token_age_s": 30, "max_entry_gain_pct": 25, "max_creator_launches": 2, "block_collapsed_creators": True},
        {"min_trades": 0, "max_token_age_s": 5, "max_entry_gain_pct": 0, "max_creator_launches": 0},
        {"ladder": []}, {"ladder": [{"gain_pct": 30, "sell_pct": 50}]}, {"ladder": [{"gain_pct": 30, "sell_pct": 50}, {"gain_pct": 60, "sell_pct": 25.5}], "ladder_breakeven_stop": False},
        {"ladder": [{"gain_pct": 10, "sell_pct": 20}] * 1 + [{"gain_pct": 20, "sell_pct": 20}, {"gain_pct": 30, "sell_pct": 20}, {"gain_pct": 40, "sell_pct": 20}, {"gain_pct": 50, "sell_pct": 20}]},
    ]
    bad = [
        {"amount_min": 1, "amount_max": 0.5}, {"stop_loss": 100}, {"stop_loss": 0}, {"take_profit": True}, {"take_profit": "30"},
        {"take_profit": 1e9}, {"nonsense": 1}, {"zzz": 1, "aaa": 2}, {"verdicts": []}, {"verdicts": ["REJECT"]}, {"verdicts": "PASS"},
        {"exit_on_creator_sell": 1}, {"auto_compound": "yes"}, {"use_blacklist": None}, {"honeypot_detection": 0}, {"max_open": 0},
        {"latency_ms": -1}, {"tx_cost_sol": 5}, {"size_mode": "yolo"}, {"slippage_pct": 0}, {"slippage_pct": 51}, {"trailing_stop_pct": 100},
        {"min_liquidity_sol": -1}, {"blacklist": ["nope"]}, {"blacklist": "x"}, {"blacklist": [1]}, {"blacklist": [WALLET] * 201},
        {"amount_min": 0}, {"amount_max": 101}, {"fee_bps": 1001}, {"max_hold_s": 5}, {"max_entry_mcap_sol": 0.5},
        {"exit_mode": "model"}, {"exit_mode": "magic"}, {"entry_mode": "yolo"}, {"confirm_gain_pct": 501}, {"confirm_window_s": 4}, {"confirm_min_flow_sol": -1},
        {"ladder": "x"}, {"ladder": [{"gain_pct": g, "sell_pct": 5} for g in range(10, 70, 10)]}, {"ladder": [{"gain_pct": 30}]}, {"ladder": [{"gain_pct": 30, "sell_pct": 50, "x": 1}]},
        {"ladder": [{"gain_pct": "30", "sell_pct": 50}]}, {"ladder": [{"gain_pct": True, "sell_pct": 50}]}, {"ladder": [30]}, {"ladder": [{"gain_pct": 60, "sell_pct": 50}, {"gain_pct": 30, "sell_pct": 25}]},
        {"ladder": [{"gain_pct": 30, "sell_pct": 50}, {"gain_pct": 30, "sell_pct": 25}]}, {"ladder": [{"gain_pct": 30, "sell_pct": 60}, {"gain_pct": 60, "sell_pct": 41}]}, {"ladder": [{"gain_pct": 0, "sell_pct": 50}]},
        {"ladder": [{"gain_pct": 1001, "sell_pct": 50}]}, {"ladder": [{"gain_pct": 30, "sell_pct": 0}]}, {"ladder": [{"gain_pct": 30, "sell_pct": 101}]}, {"ladder_breakeven_stop": "no"},
        {"min_trades": -1}, {"min_trades": 1001}, {"max_token_age_s": 4}, {"max_entry_gain_pct": -1}, {"max_creator_launches": 1001}, {"block_collapsed_creators": "yes"},
    ]
    out = []
    for has_model in (False, True):
        for payload in good + bad + [{"exit_mode": "model"}]:
            try:
                expected = {"ok": settings_dict(parse_settings(payload, default_strategy(), has_model))}
            except ValueError as e:
                expected = {"error": str(e)}
            out.append({"payload": payload, "has_model": has_model, "expected": expected})
    return out


T0 = 1_000_000  # nonzero so the "bot started" log line never falls back to the wall clock


def handcrafted():
    """Small streams that force each exit and skip path the random demo market rarely reaches."""
    from sniper.pump.synth import Sim

    quick = {"latency_ms": 0, "tx_cost_sol": 0}
    out = []

    def sim(mint="M1", creator="CREATOR", t=T0):
        return Sim(mint, creator, t)

    x = sim()
    out.append(("stop-loss", quick, [x.create(), x.buy(T0 + 50, 10.0), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.sell(T0 + 300, 2.0e8, "D"), x.buy(T0 + 400, 0.1)]))
    x = sim(); launch = x.create(dev_sol=1.0)
    out.append(("creator-sold", quick, [launch, x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.sell(T0 + 300, launch.dev_tokens * 0.1, "CREATOR"), x.buy(T0 + 400, 0.1)]))
    x = sim()
    out.append(("graduated", {**quick, "take_profit": 1000, "max_entry_mcap_sol": 100}, [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.buy(T0 + 300, 90.0), x.buy(T0 + 400, 0.1)]))
    x = sim()
    out.append(("slippage-abort", {"latency_ms": 500, "tx_cost_sol": 0, "slippage_pct": 5}, [x.create(), x.scan(T0 + 100), x.buy(T0 + 300, 1.0), x.buy(T0 + 500, 5.0), x.buy(T0 + 700, 0.01)]))
    x = sim()
    out.append(("trailing-stop", {**quick, "take_profit": 1000, "stop_loss": 99, "trailing_stop_pct": 10},
                [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.buy(T0 + 300, 6.0), x.sell(T0 + 400, 5.0e7, "O"), x.buy(T0 + 500, 0.01)]))
    x = sim()
    out.append(("timeout", {**quick, "max_hold_s": 10}, [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.buy(T0 + 60_000, 0.2)]))
    x = sim()
    out.append(("low-liquidity", {**quick, "min_liquidity_sol": 5}, [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.2)]))
    x = sim(); y = sim("M2", "CREATOR", T0 + 100_000)
    out.append(("compounding-sizes", {**quick, "size_mode": "fixed", "max_hold_s": 30},
                [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 1.0), x.buy(T0 + 300, 12.0), x.buy(T0 + 400, 0.5),
                 y.create(), y.scan(T0 + 100_100), y.buy(T0 + 100_200, 0.3), y.buy(T0 + 100_300, 0.3)]))
    x = sim("M1", "RUGGER"); launch = x.create(dev_sol=2.0); dump = x.sell(T0 + 300, launch.dev_tokens, "RUGGER"); y = sim("M2", "RUGGER", T0 + 5000)
    out.append(("rugger-then-relaunch", quick, [launch, dump, y.create(), y.scan(T0 + 5100), y.buy(T0 + 5200, 0.2)]))
    x = sim()
    out.append(("trailing-never-armed", {**quick, "take_profit": 1000, "stop_loss": 99, "trailing_stop_pct": 5},
                [x.create(), x.buy(T0 + 50, 8.0), x.scan(T0 + 100), x.buy(T0 + 200, 0.01), x.sell(T0 + 300, 1.5e8, "X"), x.buy(T0 + 400, 0.01)]))
    x = sim("M1", "PARTIAL", T0); launch = x.create(dev_sol=2.0); part = x.sell(T0 + 300, launch.dev_tokens * 0.6, "PARTIAL"); y = sim("M2", "PARTIAL", T0 + 5000)
    out.append(("creator-sells-60-percent", quick, [launch, part, y.create(), y.scan(T0 + 5100), y.buy(T0 + 5200, 0.2)]))
    day = 86_400_000
    x = sim("M1", "CREATOR", T0); y = sim("M2", "OTHER", T0 + 2 * day)
    out.append(("older-than-24h", quick, [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 1.0), x.buy(T0 + 300, 12.0), x.buy(T0 + 400, 0.5), y.create(), y.scan(T0 + 2 * day + 100), y.buy(T0 + 2 * day + 200, 0.2)]))
    # ---- decision-system paths: smart exits, stalled tokens, confirm entry, learned model ----
    def pump_fade(mint, t0):
        x = sim(mint, "C", t0)
        return [x.create(), x.scan(t0 + 100), x.buy(t0 + 200, 1.0), x.buy(t0 + 300, 12.0), x.sell(t0 + 9_000, 3.0e7, "OTHER"), x.buy(t0 + 9_500, 0.01)]

    out.append(("smart-pump-then-fade", {**quick, "exit_mode": "smart"}, pump_fade("M1", T0)))
    x = sim()
    out.append(("smart-loser", {**quick, "exit_mode": "smart"}, [x.create(), x.buy(T0 + 50, 10.0), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.sell(T0 + 300, 2.0e8, "D"), x.buy(T0 + 400, 0.1)]))
    x = sim(); y = sim("M2", "C2", T0 + 80_000)
    out.append(("smart-stalled-winner", {**quick, "exit_mode": "smart", "take_profit": 1000},
                [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 1.0), x.buy(T0 + 300, 12.0), y.create(), y.scan(T0 + 80_100), y.buy(T0 + 80_200, 0.01)]))
    x = sim(); y = sim("M2", "C2", T0 + 100_000)
    out.append(("confirm-watch-expires", {**quick, "entry_mode": "confirm", "confirm_window_s": 30}, [x.create(), x.scan(T0 + 100), x.buy(T0 + 500, 0.01), y.create()]))
    x = sim()
    out.append(("confirm-enters-after-buyers", {**quick, "entry_mode": "confirm", "confirm_gain_pct": 10, "confirm_window_s": 30},
                [x.create(), x.scan(T0 + 100), x.buy(T0 + 500, 0.01), x.buy(T0 + 5_000, 2.0), x.buy(T0 + 6_000, 0.01), x.buy(T0 + 7_000, 0.01)]))
    x = sim()
    out.append(("confirm-needs-current-flow", {**quick, "entry_mode": "confirm", "confirm_min_flow_sol": 0.5, "confirm_window_s": 60},
                [x.create(), x.buy(T0 + 500, 3.0), x.scan(T0 + 30_000), x.buy(T0 + 35_000, 0.01)]))
    out.append(("confirm-plus-smart", {**quick, "entry_mode": "confirm", "confirm_gain_pct": 5, "exit_mode": "smart"}, pump_fade("M1", T0)))
    out.append(("model-exit-on-fade", {**quick, "exit_mode": "model"}, pump_fade("M1", T0), True))

    # ---- entry filters ----
    def tok(mint, creator, t0, moves=(), scan_at=100):
        x = sim(mint, creator, t0)
        ev = [x.create()]
        for dt, sol in moves:
            ev.append(x.buy(t0 + dt, sol) if sol > 0 else x.sell(t0 + dt, -sol, "O"))
        return ev + [x.scan(t0 + scan_at), x.buy(t0 + scan_at + 5_000, 0.01)]

    # ---- take-profit ladders ----
    LAD = [{"gain_pct": 30, "sell_pct": 50}, {"gain_pct": 60, "sell_pct": 25}]

    def lad(steps, mint="M1"):
        x = sim(mint, "C", T0)
        ev = [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.01)]
        for dt, v in steps:
            ev.append(x.buy(T0 + dt, v) if v > 0 else x.sell(T0 + dt, -v, "O"))
        return ev

    out.append(("ladder-two-rungs", {**quick, "ladder": LAD, "stop_loss": 20}, lad([(1_000, 5.0), (2_000, 0.01), (3_000, 5.0), (4_000, 0.01)])))
    out.append(("ladder-one-jump", {**quick, "ladder": LAD}, lad([(1_000, 12.0), (2_000, 0.01), (3_000, 5.0)])))
    out.append(("ladder-closes-position", {**quick, "ladder": [{"gain_pct": 30, "sell_pct": 50}, {"gain_pct": 60, "sell_pct": 50}]}, lad([(1_000, 5.0), (2_000, 0.01), (3_000, 5.0), (4_000, 0.01)])))
    out.append(("ladder-breakeven-stop", {**quick, "ladder": LAD}, lad([(1_000, 5.0), (2_000, 0.01), (3_000, -2.4e8), (4_000, 0.01)])))
    out.append(("ladder-no-breakeven", {**quick, "ladder": LAD, "ladder_breakeven_stop": False, "stop_loss": 90}, lad([(1_000, 5.0), (2_000, 0.01), (3_000, -2.4e8), (4_000, 0.01)])))
    out.append(("ladder-trailing-remainder", {**quick, "ladder": LAD, "trailing_stop_pct": 10}, lad([(1_000, 5.0), (2_000, 0.01), (3_000, 8.0), (4_000, 0.01), (5_000, -9.0e7), (6_000, 0.01)])))
    out.append(("ladder-with-costs-and-delay", {"ladder": LAD, "latency_ms": 500, "tx_cost_sol": 0.003, "ladder_breakeven_stop": False, "stop_loss": 90}, lad([(1_000, 5.0), (2_000, 0.01), (3_000, 5.0), (4_000, 0.01), (6_000, 0.01)])))
    out.append(("ladder-smart-exit", {**quick, "ladder": LAD, "exit_mode": "smart"}, lad([(1_000, 5.0), (2_000, 0.01), (20_000, -3.0e7), (21_000, 0.01)])))
    out.append(("ladder-stop-before-any-rung", {**quick, "ladder": LAD}, [(lambda x: [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.01), x.buy(T0 + 1_000, 2.0), x.sell(T0 + 3_000, 2.2e8, "O"), x.buy(T0 + 4_000, 0.01)])(sim())][0]))
    # a rung is requested, then the price moves before the sale fills (500 ms delay)
    for tag, crash in (("small", -9.0e7), ("medium", -1.6e8), ("big", -4.0e8)):
        x = sim()
        out.append((f"ladder-price-falls-before-fill-{tag}", {"ladder": LAD, "latency_ms": 500, "tx_cost_sol": 0.0, "stop_loss": 20},
                    [x.create(), x.buy(T0 + 10, 10.0), x.scan(T0 + 100), x.buy(T0 + 700, 0.01), x.buy(T0 + 1_000, 10.0), x.sell(T0 + 1_200, -crash, "O"), x.buy(T0 + 1_600, 0.01), x.buy(T0 + 3_000, 0.01)]))
    out.append(("market-ladder", {"ladder": LAD, "trailing_stop_pct": 12, "latency_ms": 300, "tx_cost_sol": 0.002}, None))
    out.append(("market-ladder-smart-confirm", {"ladder": [{"gain_pct": 20, "sell_pct": 30}, {"gain_pct": 50, "sell_pct": 30}], "exit_mode": "smart", "entry_mode": "confirm", "confirm_gain_pct": 5}, None))
    out.append(("filter-min-trades", {**quick, "min_trades": 2}, tok("A", "C1", T0) + tok("B", "C2", T0 + 100_000, [(10, 0.2), (20, 0.2)])))
    out.append(("filter-min-trades-boundary", {**quick, "min_trades": 2}, tok("A", "C1", T0, [(10, 0.2)]) + tok("B", "C2", T0 + 100_000, [(10, 0.2), (20, 0.2)])))
    out.append(("filter-serial-24h-window", {**quick, "max_creator_launches": 0}, tok("OLD", "C1", T0) + tok("NEW", "C1", T0 + 2 * 86_400_000) + tok("NEWER", "C1", T0 + 2 * 86_400_000 + 60_000)))
    out.append(("filter-serial-later-launch-ignored", {**quick, "max_creator_launches": 0}, tok("A", "C1", T0, scan_at=30_000) + tok("B", "C1", T0 + 10_000, scan_at=30_000)))
    out.append(("filter-token-age", {**quick, "max_token_age_s": 10}, tok("A", "C1", T0, scan_at=30_000) + tok("B", "C2", T0 + 100_000, scan_at=5_000)))
    out.append(("filter-no-chasing", {**quick, "max_entry_gain_pct": 20}, tok("A", "C1", T0, [(10, 8.0)]) + tok("B", "C2", T0 + 100_000)))
    serial = []
    for i in range(5):
        serial += tok(f"S{i}", "SERIAL", T0 + i * 60_000, [(10, 0.2)])
    out.append(("filter-serial-creator", {**quick, "max_creator_launches": 2}, serial + tok("OTHER", "FRESH", T0 + 400_000)))
    x = sim("P1", "CC", T0)
    prior = [x.create(), x.scan(T0 + 100), x.buy(T0 + 10, 8.0), x.sell(T0 + 30_000, 2.2e8, "O")]
    out.append(("filter-collapsed-creator", {**quick, "block_collapsed_creators": True}, prior + tok("NEXT", "CC", T0 + 200_000)))
    x = sim("P2", "KEEP", T0)
    kept = [x.create(), x.scan(T0 + 100), x.buy(T0 + 10, 8.0)]
    out.append(("filter-creator-kept-its-gain", {**quick, "block_collapsed_creators": True}, kept + tok("NEXT2", "KEEP", T0 + 200_000)))
    out.append(("filters-with-confirm", {**quick, "entry_mode": "confirm", "confirm_gain_pct": 10, "max_entry_gain_pct": 30}, tok("A", "C1", T0, [(10, 2.0), (20, 6.0)])))
    out.append(("market-all-filters", {"min_trades": 0, "max_token_age_s": 120, "max_entry_gain_pct": 400, "max_creator_launches": 6, "block_collapsed_creators": True, "max_dev_buy_pct": 20, "latency_ms": 300}, None))
    x = sim()
    out.append(("smart-high-target-keeps-running", {**quick, "exit_mode": "smart", "take_profit": 200},
                [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.buy(T0 + 300, 25.0), x.buy(T0 + 400, 0.5), x.sell(T0 + 12_000, 3.0e7, "O"), x.buy(T0 + 12_500, 0.01)]))
    return out


def tuner_scenarios(market, no_random):
    from .test_tuner import pump_and_dump

    def cfg(**kw):
        return {"enabled": True, "clones": 6, "round_launches": 50, "caution": "eager", "blend": 0.5, "params": list(DEFAULT_PARAMS), **kw}

    quick = {"latency_ms": 0, "tx_cost_sol": 0, "max_open": 20, "max_hold_s": 30, "take_profit": 60, "size_mode": "fixed", "amount_min": 0.1, "amount_max": 0.1}
    fixed = {"size_mode": "fixed"}
    return [  # (name, settings, bankroll, events, uses_model, tuner settings)
        ("tuner-market-eager", fixed, 5.0, market, False, cfg()),
        ("tuner-8-clones-fast-rounds", {**no_random("aggressive")}, 50.0, market, False, cfg(clones=8, round_launches=30, blend=0.7)),
        ("tuner-smart-confirm", {**fixed, "exit_mode": "smart", "entry_mode": "confirm", "confirm_gain_pct": 6, "max_entry_gain_pct": 120}, 5.0, market, False, cfg(round_launches=40)),
        ("tuner-ladder", {**PRESETS["ladder"]["settings"], **fixed}, 5.0, market, False, cfg()),
        ("tuner-strict-3-clones", fixed, 5.0, market, False, cfg(clones=3, caution="strict", round_launches=60)),
        ("tuner-careful-filters", {**fixed, "max_entry_gain_pct": 150, "min_liquidity_sol": 0.1}, 5.0, market, False, cfg(caution="careful")),
        ("tuner-pump-and-dump", quick, 5.0, pump_and_dump(160, start=1_700_000_000_000), False, cfg(round_launches=40, caution="balanced", params=["take_profit"])),
        ("tuner-pump-and-dump-all", quick, 5.0, pump_and_dump(160, start=1_700_000_000_000), False, cfg(round_launches=40, caution="eager")),
        ("tuner-model-exit", {**fixed, "exit_mode": "model", "latency_ms": 500}, 5.0, market, True, cfg()),
        ("tuner-subset-params", fixed, 5.0, market, False, cfg(params=["stop_loss", "max_hold_s"], clones=4)),
        ("tuner-off", fixed, 5.0, market, False, {"enabled": False}),
        ("tuner-many-rounds", quick, 5.0, pump_and_dump(700, start=1_700_000_000_000), False, cfg(clones=2, round_launches=20, params=[])),
    ]


def tuner_cases():
    """Pure pieces of the tuner on awkward inputs, with what Python makes of them."""
    import random
    rnd = random.Random(5)
    out = {"rng": {str(seed): [mulberry32(seed)() for _ in range(1)] and (lambda g: [g() for _ in range(40)])(mulberry32(seed)) for seed in (0, 1, 5, 20261014, 4294967295, 123456789)}}
    out["snap"] = [[x, p, snap(x, p)] for x, p in [(0.7000000000000001, 0.1), (32.74, 0.5), (32.75, 0.5), (7.5, 1), (1234, 5), (0, 0.5), (19.95, 0.1), (149.999, 0.5), (2.675, 0.01 * 10)] +
                   [(rnd.uniform(0, 200), rnd.choice([0.1, 0.5, 1, 5])) for _ in range(60)]]
    gates = []
    for i in range(8):
        n = rnd.choice([20, 50, 100])
        diffs = [[(rnd.uniform(-40, 60) if rnd.random() < [0.1, 0.3, 0.8][i % 3] else 0.0) for _ in range(n)] for _ in range(rnd.choice([1, 3, 6]))]
        alpha, seed = rnd.choice([0.1 / 6, 0.05, 0.01 / 8, 0.2]), rnd.randrange(1, 10**6)
        gates.append({"diffs": diffs, "alpha": alpha, "seed": seed, "expected": [list(r) for r in paired_gate(diffs, alpha, mulberry32(seed))]})
    out["gate"] = gates
    from sniper.paper.engine import Strategy
    strategies = [{}, {"take_profit_pct": 300.0}, {"exit_mode": "smart", "entry_mode": "confirm", "max_entry_gain_pct": 80.0}, {"ladder": ((30.0, 50.0), (60.0, 25.0))},
                  {"exit_mode": "model"}, {"stop_loss_pct": 58.0, "min_trades": 29, "max_dev_buy_pct": 1.0, "trailing_stop_pct": 39.0}]
    blends = []
    for fields in strategies:
        st = Strategy(**fields)
        for winners in ([({"take_profit": 40.0}, 3.0), ({"take_profit": 32.0, "stop_loss": 10.0}, 1.0)], [({"stop_loss": 90.0, "min_trades": 40.0}, 2.0)], []):
            tunables = Tuner(TunerConfig(enabled=True)).active_tunables(st)
            blends.append({"fields": {k: ([list(r) for r in v] if k == "ladder" else v) for k, v in fields.items()}, "winners": [[w, g] for w, g in winners], "blend": rnd.choice([0.1, 0.5, 1.0]),
                           "expected": blend_changes(st, tunables, winners, 0.5)})
    for b in blends:
        st = Strategy(**{k: (tuple(tuple(r) for r in v) if k == "ladder" else v) for k, v in b["fields"].items()})
        b["expected"] = blend_changes(st, Tuner(TunerConfig(enabled=True)).active_tunables(st), [(w, g) for w, g in b["winners"]], b["blend"])
    out["blend"] = blends
    plans = []
    for fields in strategies:
        for cfg in (TunerConfig(enabled=True, clones=6), TunerConfig(enabled=True, clones=3, params=("take_profit", "stop_loss")), TunerConfig(enabled=True, clones=8, params=("min_trades", "max_dev_buy_pct"))):
            tuner = Tuner(cfg, seed=77)
            st = Strategy(**fields)
            plans.append({"fields": {k: ([list(r) for r in v] if k == "ladder" else v) for k, v in fields.items()}, "cfg": {"enabled": True, "clones": cfg.clones, "round_launches": cfg.round_launches,
                          "caution": cfg.caution, "blend": cfg.blend, "params": list(cfg.params)}, "expected": [tuner._plan(st, tuner.active_tunables(st)) for _ in range(3)]})
    out["plan"] = plans
    parses = []
    base = TunerConfig()
    for data in [{}, {"enabled": True}, {"clones": 4, "caution": "strict"}, {"blend": 0.25, "params": ["stop_loss", "take_profit"]}, {"params": []}, {"enabled": "yes"}, {"clones": 1}, {"clones": 9}, {"clones": 2.5},
                 {"clones": True}, {"round_launches": 10}, {"round_launches": 1001}, {"caution": "yolo"}, {"blend": 0}, {"blend": 1.5}, {"blend": True}, {"params": ["nope"]}, {"params": ["stop_loss", "stop_loss"]},
                 {"params": "stop_loss"}, {"surprise": 1}, {"clones": "6"}, {"round_launches": 100.0}, {"caution": ["x"]}, {"caution": 5}, {"caution": None}, {"caution": {"a": 1}},
                 {"params": [["take_profit"]]}, {"params": [5]}, {"params": [None]}, {"params": ["take_profit", ["stop_loss"]]}, {"enabled": 1}, {"enabled": None}]:
        try:
            cfg = parse_tuner(data, base)
            parses.append({"data": data, "expected": {"ok": {"enabled": cfg.enabled, "clones": cfg.clones, "round_launches": cfg.round_launches, "caution": cfg.caution, "blend": cfg.blend, "params": list(cfg.params)}}})
        except ValueError as e:
            parses.append({"data": data, "expected": {"error": str(e)}})
    out["parse"] = parses
    out["passes"] = [[ch, mean, lcb, k, clone_passes(ch, mean, lcb, k)] for ch in ({}, {"take_profit": 40.0})
                     for mean, lcb, k in [(2.0, 0.5, 5), (2.0, 0.5, 4), (2.0, 0.0, 9), (2.0, -0.1, 9), (0.0, 0.5, 9), (-1.0, 0.5, 9), (2.0, 1e-12, 9)]]
    out["alpha"] = [[c, n, alpha_for(c, n)] for c in ("eager", "balanced", "careful", "strict") for n in (2, 3, 6, 8)]
    out["judge"] = judge_cases()
    out["script"] = script_cases()
    return out


QUICK_PAYLOAD = {"latency_ms": 0, "tx_cost_sol": 0, "max_open": 20, "max_hold_s": 30, "take_profit": 60, "size_mode": "fixed", "amount_min": 0.1, "amount_max": 0.1}


def judge_cases():
    """Round-end decisions with the statistics scripted, so every rule is compared on its own."""
    from .test_tuner import pump_and_dump
    events = pump_and_dump(30, start=1_700_000_000_000)
    specs = [
        ("one winner", [[3.0, 1.0, 9], [3.0, -1.0, 9], [3.0, -1.0, 9], [3.0, -1.0, 9]], False, "balanced"),
        ("nobody clears the bar", [[3.0, -1.0, 9]] * 4, False, "strict"),
        ("win too small to apply", [[3.0, 1.0, 9]] * 4, True, "careful"),
        ("weighted winners", [[2.0, 0.5, 8], [6.0, 2.0, 12], [1.0, -0.1, 30], [0.5, 0.3, 4]], False, "eager"),
    ]
    out = []
    for name, gate, empty_blend, caution in specs:
        cfg = TunerConfig(enabled=True, clones=4, round_launches=20, params=("take_profit", "stop_loss"), caution=caution)
        tuner = Tuner(cfg, gate=lambda diffs, alpha, rng, counts=None, g=gate: [tuple(x) for x in g], blender=(lambda *a: {}) if empty_blend else None)
        engine = PaperEngine(parse_settings(QUICK_PAYLOAD, default_strategy()), 1.0)
        verdict = None
        for ev in events:
            engine.on_event(ev)
            verdict = tuner.feed(ev, engine)
            if verdict:
                break
        out.append({"name": name, "events": [wire(e) for e in events], "payload": QUICK_PAYLOAD, "gate": gate, "empty_blend": empty_blend,
                    "cfg": {"enabled": True, "clones": 4, "round_launches": 20, "caution": caution, "blend": 0.5, "params": ["take_profit", "stop_loss"]}, "expected": verdict})
    return out


def script_cases():
    """The terminal's own handling of the tuner (stop/start, hand edits, repeat laps, reset), step by step."""
    from .test_tuner import pump_and_dump
    events = pump_and_dump(90, start=1_700_000_000_000)
    cfg = {"enabled": True, "clones": 4, "round_launches": 20, "caution": "balanced", "params": ["take_profit"]}
    scripts = {
        "stop-start-edit-lap-reset": [
            {"configure": cfg}, {"emit": [0, 60]}, {"running": True}, {"emit": [60, 130]}, {"settings": {"take_profit": 45}}, {"emit": [130, 170]},
            {"running": False}, {"emit": [170, 200]}, {"running": True}, {"emit": [200, 260]}, {"lap": 2}, {"emit": [260, 300]}, {"lap": 1}, {"emit": [300, 400]},
            {"reset": True}, {"emit": [400, 450]}, {"configure": {"enabled": False}}, {"emit": [440, 450]},
        ],
        "walks-the-target-down": [{"configure": cfg}, {"running": True}, {"emit": [0, 150]}, {"emit": [150, 300]}, {"emit": [300, 450]}],
        "reconfigure-midway": [
            {"running": True}, {"configure": {**cfg, "round_launches": 40}}, {"emit": [0, 120]}, {"configure": {"clones": 6, "caution": "eager", "blend": 0.8}}, {"emit": [120, 300]},
            {"configure": {"params": ["take_profit", "stop_loss", "max_hold_s"]}}, {"emit": [300, 450]},
        ],
        "bad-config-leaves-everything": [
            {"configure": cfg}, {"running": True}, {"emit": [0, 50]}, {"configure_bad": {"clones": 99}}, {"emit": [50, 100]}, {"configure_bad": {"caution": ["x"]}}, {"configure_bad": {"params": [["take_profit"]]}},
        ],
    }
    out = []
    for name, steps in scripts.items():
        st = TerminalState(default_strategy(), source="demo", simulated=True, start_sol=5.0)
        st.update_settings(QUICK_PAYLOAD)
        st.clock = events[0].t - 1000
        observed = []
        for step in steps:
            (kind, arg), = step.items()
            if kind == "configure":
                st.configure_tuner(arg)
            elif kind == "configure_bad":
                try:
                    st.configure_tuner(arg)
                except ValueError:
                    pass
            elif kind == "emit":
                for ev in events[arg[0]:arg[1]]:
                    st.emit(ev)
            elif kind == "running":
                st.set_running(arg)
            elif kind == "settings":
                st.update_settings(arg)
            elif kind == "lap":
                st.set_lap(arg)
            elif kind == "reset":
                st.reset()
            snap_ = st.snapshot("all")
            observed.append(json.loads(json.dumps({"tuner": snap_["tuner"], "settings": snap_["settings"], "running": snap_["running"], "paper": snap_["paper"], "lap": snap_["lap"], "stats": snap_["stats"],
                                       "notes": [l["text"] for l in st.logs if l["kind"] in ("tune", "info")]})))
        out.append({"name": name, "payload": QUICK_PAYLOAD, "events": [wire(e) for e in events], "steps": steps, "observed": observed})
    return out


def run_cases():
    market = sorted(synth.generate(11, 400, 1_700_000_000_000), key=lambda e: e.t)
    no_random = lambda name: {**PRESETS[name]["settings"], "size_mode": "fixed"}  # JS has its own random numbers
    scenarios = [  # (name, settings payload, bankroll, events)
        ("defaults", {}, 1.0, market),
        ("steady", PRESETS["steady"]["settings"], 1.0, market),
        ("balanced-fixed", no_random("balanced"), 50.0, market),
        ("aggressive-fixed", no_random("aggressive"), 200.0, market),
        ("degen-fixed", no_random("degen"), 500.0, market),
        ("trail-slip-liquidity", {"trailing_stop_pct": 15, "slippage_pct": 3, "min_liquidity_sol": 1}, 1.0, market),
        ("no-protection", {"honeypot_detection": False, "exit_on_creator_sell": False}, 1.0, market),
        ("no-blacklist-memory", {"use_blacklist": False}, 1.0, market),
        ("slow-compounding", {"latency_ms": 2000, "auto_compound": True, "max_open": 6}, 1.0, market),
        ("wide-verdicts", {"verdicts": ["PASS", "CAUTION", "AVOID"], "take_profit": 80, "stop_loss": 30, "max_hold_s": 900}, 5.0, market),
        ("tiny-bankroll-sizes", {"amount_min": 0.4, "amount_max": 0.9, "size_mode": "fixed", "auto_compound": False}, 1.0, market),
        ("tight-stops", {"take_profit": 8, "stop_loss": 2, "max_hold_s": 60, "latency_ms": 0}, 1.0, market),
        ("market-smart-exit", {"exit_mode": "smart", "take_profit": 30}, 5.0, market),
        ("market-confirm-entry", {"entry_mode": "confirm", "confirm_gain_pct": 8}, 5.0, market),
        ("market-confirm-smart", {"entry_mode": "confirm", "confirm_gain_pct": 5, "exit_mode": "smart", "latency_ms": 300}, 5.0, market),
        ("market-model-exit", {"exit_mode": "model", "latency_ms": 500}, 5.0, market, True),
        ("market-model-confirm", {"exit_mode": "model", "entry_mode": "confirm", "confirm_gain_pct": 5}, 5.0, market, True),
    ] + [(entry[0], entry[1], 100.0, market if entry[2] is None else entry[2], *entry[3:]) for entry in handcrafted()] + tuner_scenarios(market, no_random)
    runs = []
    model = fixed_model()
    for name, payload, bankroll, events, *extra in scenarios:
        use_model = bool(extra and extra[0])
        tuner_cfg = extra[1] if len(extra) > 1 else None
        events = sorted(events, key=lambda e: e.t)
        st = TerminalState(default_strategy(), source="demo", simulated=True, start_sol=bankroll, model_policy=ModelExit(model) if use_model else None)
        st.update_settings(payload)
        st.clock = events[0].t - 1000  # fixes the timestamp on the "bot started" log line
        clock0 = st.clock
        if tuner_cfg:
            st.configure_tuner(tuner_cfg)
        st.set_running(True)
        for ev in events:
            st.emit(ev)
        runs.append({
            "name": name, "payload": payload, "tuner": tuner_cfg, "start_sol": bankroll, "clock0": clock0, "model": model_json(model) if use_model else None, "events": [wire(e) for e in events],
            "snapshots": {k: json.loads(json.dumps(st.snapshot(k.split("@")[0], int(k.split("@")[1]) if "@" in k else DEFAULT_ROWS)))
                          for k in ("24h", "all", "all@1", "all@3", "all@100")},
            "closed": [dataclasses.asdict(c) for c in st.engine.closed],
        })
    return runs


def lap_cases():
    """A recording and what its repeat laps must look like (the browser build repeats it the same way)."""
    from sniper.terminal.feeds import repeat_lap
    events = sorted(synth.generate(3, 12, 1_000_000, 0), key=lambda e: e.t)
    return {"events": [wire(e) for e in events], "laps": {str(n): [wire(e) for e in repeat_lap(events, n)] for n in (2, 3, 5)}}


def fork_cases():
    """Strategies (as settings screen payloads) and the trades Python makes with them on one market."""
    market = sorted(synth.generate(11, 90, 1_000_000), key=lambda e: e.t)
    payloads = {
        "default": {"latency_ms": 300},
        "trail+ladder": {"latency_ms": 300, "trailing_stop_pct": 12, "take_profit": 80, "ladder": [{"gain_pct": 15, "sell_pct": 40}, {"gain_pct": 40, "sell_pct": 30}]},
        "confirm+smart": {"latency_ms": 300, "entry_mode": "confirm", "confirm_gain_pct": 6, "exit_mode": "smart"},
        "filters": {"latency_ms": 300, "min_trades": 2, "max_entry_gain_pct": 60, "block_collapsed_creators": True, "max_creator_launches": 2, "min_liquidity_sol": 0.5},
        "no-scan": {"latency_ms": 0, "tx_cost_sol": 0.001, "honeypot_detection": False},
        "compounding": {"latency_ms": 300, "auto_compound": True, "max_open": 5, "size_mode": "fixed"},
    }
    cases = []
    for name, payload in payloads.items():
        engine = PaperEngine(parse_settings(payload, default_strategy()), 1.0)
        for ev in market:
            engine.on_event(ev)
        cases.append({"name": name, "payload": payload, "events": [wire(e) for e in market], "expected": [dataclasses.asdict(c) for c in engine.finish().closed]})
    sim = synth.Sim("M1", "C1", 1000)
    slip = [sim.create(), sim.scan(1100), sim.buy(1300, 1.0), sim.buy(1500, 5.0), sim.buy(1700, 0.01)]
    sim = synth.Sim("M1", "RUGGER", 1000)
    launch = sim.create(dev_sol=2.0)
    rug = [launch, sim.scan(1100), sim.sell(1300, launch.dev_tokens * 0.3, "RUGGER"), sim.sell(1400, launch.dev_tokens * 0.3, "RUGGER")]
    hand = [
        {"name": "order waiting to fill still checks slippage", "payload": {"latency_ms": 500, "tx_cost_sol": 0, "slippage_pct": 5}, "events": [wire(e) for e in slip], "split": 3, "expect": {"slippage": 1}},
        {"name": "creator selling in two parts", "payload": {"latency_ms": 0, "tx_cost_sol": 0}, "events": [wire(e) for e in rug], "split": 3, "expect": {"ruggers": ["RUGGER"]}},
    ]
    return {"cases": cases, "hand": hand}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsFork(unittest.TestCase):
    def test_the_browser_engine_copy_matches_the_original_and_shares_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fork.json"
            path.write_text(json.dumps(fork_cases()))
            result = subprocess.run(["node", str(ROOT / "tests/js/fork.js"), str(path)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok:", result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsParity(unittest.TestCase):
    def test_browser_build_matches_python(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cases.json"
            runs = run_cases()
            path.write_text(json.dumps({"settings": settings_cases(), "runs": runs, "features": feature_cases(), "model": model_json(fixed_model()), "laps": lap_cases(), "tuner": tuner_cases()}))
            result = subprocess.run(["node", str(ROOT / "tests/js/parity.js"), str(path)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok:", result.stdout)

    def test_the_scenarios_really_exercise_trading(self):
        """Guard against a vacuous pass: the compared runs must hit every exit and skip path."""
        runs = run_cases()
        trades = sum(r["snapshots"]["all"]["stats"]["trades"] for r in runs)
        reasons = {c["reason"] for r in runs for c in r["closed"]}
        skips = {k.split(":")[0] for r in runs for k in r["snapshots"]["all"]["counters"]["skipped"]}
        self.assertGreater(trades, 150)
        self.assertGreaterEqual(reasons, {"take_profit", "stop_loss", "timeout", "trailing_stop", "creator_sold", "graduated"})
        self.assertGreaterEqual(reasons, {"profit_stall", "smart_trail", "stalled", "model_sell"}, reasons)
        self.assertGreaterEqual(reasons, {"breakeven_stop", "ladder_2"}, reasons)
        self.assertTrue(any(l["kind"] == "partial" for r in runs for l in r["snapshots"]["all"]["logs"]), "no ladder sale was ever compared")
        # the token-row limit must be compared where it matters: more tokens than rows, and an old token still held
        self.assertTrue(any(r["snapshots"]["all@3"]["rows_total"] > 3 and len(r["snapshots"]["all@3"]["rows"]) == 3 for r in runs), "row limit never cut anything")
        self.assertTrue(any(len(r["snapshots"]["all@1"]["rows"]) > 1 for r in runs), "no older open position was ever kept past the row limit")
        tuned = [r for r in runs if r["tuner"] and r["tuner"]["enabled"]]
        rounds = [h for r in tuned for h in r["snapshots"]["all"]["tuner"]["history"]]
        self.assertGreaterEqual(len(tuned), 9)
        self.assertGreater(len(rounds), 30)
        self.assertTrue(any(h["decision"] == "changed" for h in rounds), "no tuner run ever changed a setting")
        self.assertTrue(any(h["decision"] == "kept" for h in rounds))
        self.assertTrue(any(c["passed"] for h in rounds for c in h["clones"]) and any(not c["passed"] and c["changes"] for h in rounds for c in h["clones"]))
        changed_keys = {k for h in rounds for k in h["changes"]}
        self.assertGreaterEqual(changed_keys, {"take_profit"}, changed_keys)
        self.assertTrue(any(l["kind"] == "tune" for r in tuned for l in r["snapshots"]["all"]["logs"]))
        many = next(r for r in runs if r["name"] == "tuner-many-rounds")
        self.assertEqual(len(many["snapshots"]["all"]["tuner"]["history"]), 30)
        self.assertGreater(many["snapshots"]["all"]["tuner"]["history"][0]["round"], 30)
        walk = next(s for s in script_cases() if s["name"] == "walks-the-target-down")
        self.assertTrue(any(" to " in n and "auto-tune round" in n for n in walk["observed"][-1]["notes"]), "no script ever logged a setting change")
        self.assertTrue(all(not r["snapshots"]["all"]["tuner"]["history"] for r in runs if r["tuner"] and not r["tuner"]["enabled"]))
        self.assertGreaterEqual(skips, {"max_open", "slippage", "low_liquidity", "blacklisted", "no_cash", "verdict", "no_confirmation"})
        self.assertGreaterEqual(skips, {"too_few_trades", "too_old", "chasing", "serial_creator", "collapsed_creator"}, skips)
        quiet_by_design = ("slippage-abort", "low-liquidity", "rugger-then-relaunch", "creator-sells-60-percent", "confirm-watch-expires", "confirm-needs-current-flow", "filters-with-confirm", "filter-min-trades", "filter-min-trades-boundary", "filter-serial-later-launch-ignored", "filter-token-age", "filter-no-chasing", "filter-collapsed-creator", "ladder-stop-before-any-rung", "ladder-price-falls-before-fill-small", "ladder-price-falls-before-fill-medium", "ladder-price-falls-before-fill-big")
        silent = [r["name"] for r in runs if r["snapshots"]["all"]["stats"]["trades"] + r["snapshots"]["all"]["paper"]["open"] == 0 and r["name"] not in quiet_by_design]
        self.assertEqual(silent, [], "scenarios that never traded prove nothing")


if __name__ == "__main__":
    unittest.main()
