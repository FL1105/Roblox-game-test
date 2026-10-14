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
from sniper.terminal.state import PRESETS, TerminalState, default_strategy, parse_settings, settings_dict

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


def wire(ev):
    d = dataclasses.asdict(ev)
    d["type"] = {"NewToken": "new_token", "Trade": "trade", "ScanNote": "scan"}[type(ev).__name__]
    return d


def settings_cases():
    good = [
        {}, {"take_profit": 45}, {"amount_min": 0.02, "amount_max": 0.3}, {"verdicts": ["PASS"]}, {"verdicts": ["AVOID", "PASS", "PASS"]},
        {"trailing_stop_pct": 12, "slippage_pct": 7, "min_liquidity_sol": 3, "size_mode": "random"},
        {"auto_compound": False, "use_blacklist": False, "honeypot_detection": False, "exit_on_creator_sell": False},
        {"blacklist": [WALLET, WALLET]}, {"max_open": 7.9, "latency_ms": 1234.5}, {"fee_bps": 0, "tx_cost_sol": 0.1},
        {"stop_loss": 99, "take_profit": 1000, "max_hold_s": 3600}, {"max_entry_mcap_sol": 1, "max_dev_buy_pct": 0},
        {"exit_mode": "smart"}, {"entry_mode": "confirm", "confirm_gain_pct": 25, "confirm_min_flow_sol": 0.4, "confirm_window_s": 90},
        {"confirm_gain_pct": 0, "confirm_window_s": 5, "confirm_min_flow_sol": 100},
    ]
    bad = [
        {"amount_min": 1, "amount_max": 0.5}, {"stop_loss": 100}, {"stop_loss": 0}, {"take_profit": True}, {"take_profit": "30"},
        {"take_profit": 1e9}, {"nonsense": 1}, {"zzz": 1, "aaa": 2}, {"verdicts": []}, {"verdicts": ["REJECT"]}, {"verdicts": "PASS"},
        {"exit_on_creator_sell": 1}, {"auto_compound": "yes"}, {"use_blacklist": None}, {"honeypot_detection": 0}, {"max_open": 0},
        {"latency_ms": -1}, {"tx_cost_sol": 5}, {"size_mode": "yolo"}, {"slippage_pct": 0}, {"slippage_pct": 51}, {"trailing_stop_pct": 100},
        {"min_liquidity_sol": -1}, {"blacklist": ["nope"]}, {"blacklist": "x"}, {"blacklist": [1]}, {"blacklist": [WALLET] * 201},
        {"amount_min": 0}, {"amount_max": 101}, {"fee_bps": 1001}, {"max_hold_s": 5}, {"max_entry_mcap_sol": 0.5},
        {"exit_mode": "model"}, {"exit_mode": "magic"}, {"entry_mode": "yolo"}, {"confirm_gain_pct": 501}, {"confirm_window_s": 4}, {"confirm_min_flow_sol": -1},
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
    x = sim()
    out.append(("smart-high-target-keeps-running", {**quick, "exit_mode": "smart", "take_profit": 200},
                [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.2), x.buy(T0 + 300, 25.0), x.buy(T0 + 400, 0.5), x.sell(T0 + 12_000, 3.0e7, "O"), x.buy(T0 + 12_500, 0.01)]))
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
    ] + [(entry[0], entry[1], 100.0, entry[2], *entry[3:]) for entry in handcrafted()]
    runs = []
    model = fixed_model()
    for name, payload, bankroll, events, *with_model in scenarios:
        use_model = bool(with_model and with_model[0])
        events = sorted(events, key=lambda e: e.t)
        st = TerminalState(default_strategy(), source="demo", simulated=True, start_sol=bankroll, model_policy=ModelExit(model) if use_model else None)
        st.update_settings(payload)
        st.clock = events[0].t - 1000  # fixes the timestamp on the "bot started" log line
        clock0 = st.clock
        st.set_running(True)
        for ev in events:
            st.emit(ev)
        runs.append({
            "name": name, "payload": payload, "start_sol": bankroll, "clock0": clock0, "model": model_json(model) if use_model else None, "events": [wire(e) for e in events],
            "snapshots": {w: json.loads(json.dumps(st.snapshot(w))) for w in ("24h", "all")},
            "closed": [dataclasses.asdict(c) for c in st.engine.closed],
        })
    return runs


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsParity(unittest.TestCase):
    def test_browser_build_matches_python(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cases.json"
            runs = run_cases()
            path.write_text(json.dumps({"settings": settings_cases(), "runs": runs, "features": feature_cases(), "model": model_json(fixed_model())}))
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
        self.assertGreaterEqual(skips, {"max_open", "slippage", "low_liquidity", "blacklisted", "no_cash", "verdict", "no_confirmation"})
        quiet_by_design = ("slippage-abort", "low-liquidity", "rugger-then-relaunch", "creator-sells-60-percent", "confirm-watch-expires", "confirm-needs-current-flow")
        silent = [r["name"] for r in runs if r["snapshots"]["all"]["stats"]["trades"] + r["snapshots"]["all"]["paper"]["open"] == 0 and r["name"] not in quiet_by_design]
        self.assertEqual(silent, [], "scenarios that never traded prove nothing")


if __name__ == "__main__":
    unittest.main()
