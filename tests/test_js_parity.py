"""The browser build (standalone.js) must behave exactly like the Python code it was ported from."""

import dataclasses
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sniper.pump import synth
from sniper.terminal.state import PRESETS, TerminalState, default_strategy, parse_settings, settings_dict

ROOT = Path(__file__).resolve().parent.parent
WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


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
    ]
    bad = [
        {"amount_min": 1, "amount_max": 0.5}, {"stop_loss": 100}, {"stop_loss": 0}, {"take_profit": True}, {"take_profit": "30"},
        {"take_profit": 1e9}, {"nonsense": 1}, {"zzz": 1, "aaa": 2}, {"verdicts": []}, {"verdicts": ["REJECT"]}, {"verdicts": "PASS"},
        {"exit_on_creator_sell": 1}, {"auto_compound": "yes"}, {"use_blacklist": None}, {"honeypot_detection": 0}, {"max_open": 0},
        {"latency_ms": -1}, {"tx_cost_sol": 5}, {"size_mode": "yolo"}, {"slippage_pct": 0}, {"slippage_pct": 51}, {"trailing_stop_pct": 100},
        {"min_liquidity_sol": -1}, {"blacklist": ["nope"]}, {"blacklist": "x"}, {"blacklist": [1]}, {"blacklist": [WALLET] * 201},
        {"amount_min": 0}, {"amount_max": 101}, {"fee_bps": 1001}, {"max_hold_s": 5}, {"max_entry_mcap_sol": 0.5},
    ]
    out = []
    for payload in good + bad:
        try:
            expected = {"ok": settings_dict(parse_settings(payload, default_strategy()))}
        except ValueError as e:
            expected = {"error": str(e)}
        out.append({"payload": payload, "expected": expected})
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
    ] + [(name, payload, 100.0, events) for name, payload, events in handcrafted()]
    runs = []
    for name, payload, bankroll, events in scenarios:
        events = sorted(events, key=lambda e: e.t)
        st = TerminalState(default_strategy(), source="demo", simulated=True, start_sol=bankroll)
        st.update_settings(payload)
        st.clock = events[0].t - 1000  # fixes the timestamp on the "bot started" log line
        clock0 = st.clock
        st.set_running(True)
        for ev in events:
            st.emit(ev)
        runs.append({
            "name": name, "payload": payload, "start_sol": bankroll, "clock0": clock0, "events": [wire(e) for e in events],
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
            path.write_text(json.dumps({"settings": settings_cases(), "runs": runs}))
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
        self.assertGreaterEqual(skips, {"max_open", "slippage", "low_liquidity", "blacklisted", "no_cash", "verdict"})
        quiet_by_design = ("slippage-abort", "low-liquidity", "rugger-then-relaunch", "creator-sells-60-percent")
        silent = [r["name"] for r in runs if r["snapshots"]["all"]["stats"]["trades"] + r["snapshots"]["all"]["paper"]["open"] == 0 and r["name"] not in quiet_by_design]
        self.assertEqual(silent, [], "scenarios that never traded prove nothing")


if __name__ == "__main__":
    unittest.main()
