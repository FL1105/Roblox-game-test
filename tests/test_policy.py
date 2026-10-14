import math
import os
import tempfile
import unittest
from dataclasses import replace

from sniper.paper.engine import PaperEngine, Strategy, replay
from sniper.policy import smart
from sniper.policy.dataset import token_samples
from sniper.policy.features import FEATURES, TokenTape, path_features, tape_from_launch
from sniper.policy.model import HoldModel, Logistic, ModelExit, ModelParams, auc, fit_logistic, transform
from sniper.pump.curve import CurveState, quote_buy

from .sim import Sim

S = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1)


def tape_with(*moves, launch_t=0):
    tape = TokenTape(launch_t=launch_t)
    st = CurveState(30.0, 1_073_000_000.0)
    tape.add(launch_t, st.v_sol, st.v_tokens)
    for sec, sol in moves:
        tokens = quote_buy(st, abs(sol), 125) if sol > 0 else None
        if sol > 0:
            st = CurveState(st.v_sol + sol * 0.9875, st.v_tokens - tokens)
        else:
            give = 5.0e7
            gross = st.v_sol - st.v_sol * st.v_tokens / (st.v_tokens + give)
            st = CurveState(st.v_sol - gross, st.v_tokens + give)
        tape.add(int(sec * 1000), st.v_sol, st.v_tokens)
    return tape


class Features(unittest.TestCase):
    def test_all_features_present_and_finite(self):
        f = path_features(tape_with((3, 1.0), (8, 2.0)), now=12_000)
        self.assertEqual(list(f), FEATURES)
        self.assertTrue(all(math.isfinite(v) for v in f.values()))

    def test_buying_reads_as_positive_momentum_and_flow_selling_as_negative(self):
        up = path_features(tape_with((10, 1.0), (20, 2.0)), now=22_000)
        down = path_features(tape_with((10, 1.0), (20, -1)), now=22_000)
        self.assertGreater(up["mom_5"], 0)
        self.assertGreater(up["flow_15"], 0)
        self.assertLess(down["mom_5"], 0)
        self.assertLess(down["flow_15"], 0)
        self.assertLess(down["dd_peak"], 0)
        self.assertEqual(up["dd_peak"], 0)

    def test_idle_time_grows_with_the_clock_not_with_trades(self):
        tape = tape_with((3, 1.0))
        self.assertAlmostEqual(path_features(tape, now=3_000)["idle_s"], 0.0)
        self.assertAlmostEqual(path_features(tape, now=43_000)["idle_s"], 40.0)

    def test_repeated_state_is_not_a_new_observation(self):
        tape = tape_with((3, 1.0))
        n = len(tape.ts)
        tape.add(9000, tape.vs[-1], tape.vt[-1])
        self.assertEqual(len(tape.ts), n)

    def test_tape_starts_from_the_exact_launch_state_and_dev_share(self):
        sim = Sim()
        tape = tape_from_launch(sim.create(dev_sol=2.0))
        self.assertGreater(tape.dev_pct, 1.0)
        self.assertEqual(len(tape.ts), 1)


class SmartRules(unittest.TestCase):
    P = smart.SmartParams()
    BASE = {"mom_5": 0.05, "vol_30": 0.02, "idle_s": 0.0, "flow_15": 0.5, "liq_sol": 10.0}

    def decide(self, ret, peak, **over):
        return smart.decide(self.P, ret, peak, {**self.BASE, **over})

    def test_lets_a_winner_run_while_buying_continues(self):
        self.assertIsNone(self.decide(60, 60))

    def test_takes_profit_once_momentum_fades(self):
        self.assertEqual(self.decide(45, 45, mom_5=-0.01), "profit_stall")

    def test_hard_cap(self):
        self.assertEqual(self.decide(160, 160), "take_profit")

    def test_volatility_scaled_trail(self):
        self.assertEqual(self.decide(20, 40, vol_30=0.02), "smart_trail")           # 3*0.02=6% -> floor 5%... gave back ~14%
        self.assertIsNone(self.decide(38, 40, vol_30=0.02))                          # barely off the peak
        self.assertIsNone(self.decide(20, 40, vol_30=0.10))                          # volatile token gets a wider (25%) leash
        self.assertIsNone(self.decide(10, 12))                                       # never armed (peak below arm_ret)

    def test_stalled_in_profit_and_sell_pressure(self):
        self.assertEqual(self.decide(10, 10, idle_s=40), "stalled")
        self.assertIsNone(self.decide(-5, 5, idle_s=40))                             # only banks profits
        self.assertEqual(self.decide(10, 10, flow_15=-5.0), "sell_pressure")

    def test_rules_can_be_switched_off(self):
        p = replace(self.P, use_momentum_tp=False, use_trail=False, use_stall=False, use_flow=False)
        self.assertIsNone(smart.decide(p, 45, 45, {**self.BASE, "mom_5": -0.5, "idle_s": 99, "flow_15": -9}))


class Model(unittest.TestCase):
    def test_fit_recovers_signal_and_auc_sees_it(self):
        import numpy as np
        rng = np.random.default_rng(3)
        X = rng.normal(size=(3000, 4)); y = (rng.random(3000) < 1 / (1 + np.exp(-(1.2 * X[:, 0] - 0.8 * X[:, 1])))).astype(int)
        m = fit_logistic(X, y, l2=1.0)
        self.assertGreater(m.coef[0], 0.8); self.assertLess(m.coef[1], -0.5); self.assertAlmostEqual(m.coef[3], 0.0, delta=0.15)
        self.assertGreater(auc([m.proba(list(r)) for r in X], y), 0.7)
        self.assertAlmostEqual(auc(rng.random(3000), y), 0.5, delta=0.05)

    def test_auc_edge_cases(self):
        self.assertEqual(auc([0.1, 0.9], [0, 1]), 1.0)
        self.assertEqual(auc([0.9, 0.1], [0, 1]), 0.0)
        self.assertEqual(auc([0.5, 0.5], [0, 1]), 0.5)
        self.assertTrue(math.isnan(auc([0.1, 0.2], [1, 1])))

    def test_save_load_round_trip_and_feature_guard(self):
        n = len(FEATURES)
        lg = Logistic([0.0] * n, [1.0] * n, [0.1] * n, -0.5)
        model = HoldModel(lg, lg, {"note": "test"})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            model.save(path)
            loaded = HoldModel.load(path)
            f = path_features(tape_with((3, 1.0)), now=5000)
            self.assertEqual(loaded.scores(f), model.scores(f))
            import json
            raw = json.load(open(path)); raw["features"] = raw["features"][:-1]; json.dump(raw, open(path, "w"))
            with self.assertRaises(ValueError):
                HoldModel.load(path)

    def test_transform_matches_feature_order_and_tames_tails(self):
        f = path_features(tape_with((3, 5.0)), now=200_000)
        x = transform(f)
        self.assertEqual(len(x), len(FEATURES))
        self.assertLess(x[FEATURES.index("idle_s")], 7)  # log1p of ~197 s

    def test_model_exit_sells_when_risk_outweighs_upside(self):
        n = len(FEATURES)
        danger = Logistic([0.0] * n, [1.0] * n, [0.0] * n, 3.0)   # always ~95%
        safe = Logistic([0.0] * n, [1.0] * n, [0.0] * n, -3.0)    # always ~5%
        f = path_features(tape_with((3, 1.0)), now=20_000)
        self.assertEqual(ModelExit(HoldModel(safe, danger, {})).decide(5, 5, f), "model_sell")
        self.assertIsNone(ModelExit(HoldModel(danger, safe, {})).decide(5, 5, f))
        young = path_features(tape_with((3, 1.0)), now=3_000)
        self.assertIsNone(ModelExit(HoldModel(safe, danger, {}), ModelParams(min_age_s=10)).decide(5, 5, young))


class Dataset(unittest.TestCase):
    def path(self, moves, end_reason="idle", end_t=None):
        sim = Sim("M", "C", 0)
        launch = sim.create()
        obs = []
        for sec, sol in moves:
            tr = sim.buy(int(sec * 1000), sol) if sol > 0 else sim.sell(int(sec * 1000), 5e7, "x")
            obs.append({"t": tr.t, "v_sol": tr.v_sol, "v_tokens": tr.v_tokens})
        last = obs[-1]["t"] if obs else 0
        return {"create": {"t": 0, "mint": "M", "creator": "C", "dev_tokens": 0.0, "dev_sol": 0.0, "v_sol": launch.v_sol, "v_tokens": launch.v_tokens},
                "obs": obs, "end": {"t": end_t if end_t is not None else last + 60_000, "reason": end_reason}}

    def test_pump_then_flat_labels_pump_up_and_flat_neither(self):
        rows = token_samples(self.path([(10, 4.0)]))
        by_t = {t: (up, down) for _, up, down, t in rows}
        self.assertEqual(by_t[10_000], (0, 0))          # after the pump nothing else happens
        self.assertTrue(all(up == 0 and down == 0 for up, down in by_t.values()))

    def test_a_move_inside_the_horizon_is_labelled(self):
        rows = token_samples(self.path([(10, 1.0), (20, 6.0)]))
        by_t = {t: (up, down) for _, up, down, t in rows}
        self.assertEqual(by_t[10_000], (1, 0))          # 10 s before the 6 SOL buy
        rows = token_samples(self.path([(10, 6.0), (20, -1)]))
        self.assertEqual({t: d for _, _, d, t in rows}[10_000], 1)   # a sell follows: holding loses

    def test_open_paths_drop_samples_whose_horizon_is_unobserved_idle_paths_do_not(self):
        idle = token_samples(self.path([(10, 1.0)], "idle", end_t=70_000))
        opened = token_samples(self.path([(10, 1.0)], "window_over", end_t=70_000))
        self.assertGreater(len(idle), len(opened))
        self.assertLessEqual(max(t for *_, t in opened), 70_000 - 20_000)
        self.assertLessEqual(max(t for *_, t in idle), 70_000)

    def test_no_observations_no_samples(self):
        self.assertEqual(token_samples({"create": {"t": 0, "mint": "M", "dev_tokens": 0, "dev_sol": 0, "v_sol": 30.0, "v_tokens": 1.073e9}, "obs": []}), [])


class EngineModes(unittest.TestCase):
    def events(self, with_exit_trade=True, mint="M1", t0=1_000_000):
        x = Sim(mint, "C", t0)
        ev = [x.create(), x.scan(t0 + 100), x.buy(t0 + 200, 1.0), x.buy(t0 + 300, 12.0)]
        if with_exit_trade:
            ev.append(x.sell(t0 + 9_000, 3.0e7, "OTHER"))   # buying has stopped and someone sells, with the position well in profit
        ev.append(x.buy(t0 + 9_500, 0.01))
        return ev

    def test_targets_mode_banks_the_first_30_percent(self):
        (t,) = replay(self.events(), S).closed
        self.assertEqual((t.reason, t.exit_t), ("take_profit", 1_000_300))

    def test_smart_mode_holds_through_the_pump_and_sells_when_it_fades(self):
        (t,) = replay(self.events(), replace(S, exit_mode="smart")).closed
        self.assertIn(t.reason, ("profit_stall", "smart_trail"))
        self.assertEqual(t.exit_t, 1_009_000)
        self.assertGreater(t.ret_pct, 30)

    def test_smart_mode_sells_a_stalled_winner_when_time_passes_elsewhere(self):
        a = self.events(with_exit_trade=False)[:4]
        other = Sim("M2", "C2", 1_000_000)
        later = [other.create(), other.scan(1_000_100)]
        other2 = Sim("M3", "C3", 1_060_000)
        later += [other2.create()]  # an unrelated launch a minute later: time passes with no trades in M1
        closed = replay(a + later, replace(S, exit_mode="smart", take_profit_pct=1000)).closed
        (t,) = [c for c in closed if c.mint == "M1"]
        self.assertEqual(t.reason, "stalled")

    def test_hard_stop_still_applies_in_smart_mode(self):
        x = Sim("M1", "C", 1_000_000)
        ev = [x.create(), x.buy(1_000_050, 10.0), x.scan(1_000_100), x.buy(1_000_200, 0.2), x.sell(1_000_300, 2.0e8, "D"), x.buy(1_000_400, 0.1)]
        (t,) = replay(ev, replace(S, exit_mode="smart")).closed
        self.assertEqual(t.reason, "stop_loss")

    def test_model_mode_uses_the_model_and_falls_back_to_rules_without_one(self):
        class AlwaysSell:
            def decide(self, ret, peak, f):
                return "model_sell"
        eng = PaperEngine(replace(S, exit_mode="model"), model_policy=AlwaysSell())
        for ev in self.events():
            eng.on_event(ev)
        eng.finish()
        self.assertEqual([t.reason for t in eng.closed], ["model_sell"])
        (t,) = replay(self.events(), replace(S, exit_mode="model")).closed
        self.assertIn(t.reason, ("profit_stall", "smart_trail"))


if __name__ == "__main__":
    unittest.main()


class ConfirmEntry(unittest.TestCase):
    CONFIRM = replace(S, entry_mode="confirm", confirm_gain_pct=10.0, confirm_window_s=30.0)

    def stream(self, buys, mint="M1", t0=1_000_000):
        x = Sim(mint, "C", t0)
        ev = [x.create(), x.scan(t0 + 100)]
        for dt, sol in buys:
            ev.append(x.buy(t0 + dt, sol))
        return ev

    def test_snipe_buys_immediately_confirm_waits_for_evidence(self):
        quiet = self.stream([(500, 0.01), (20_000, 0.01)])
        self.assertEqual(len(replay(quiet, S).closed), 1)                       # snipe: in at the first chance
        r = replay(quiet, self.CONFIRM)
        self.assertEqual((r.closed, r.skipped["no_confirmation"]), ([], 1))   # confirm: nobody is buying, so it never commits

    def test_confirm_enters_after_buyers_arrive_and_at_the_later_price(self):
        busy = self.stream([(500, 0.01), (5_000, 2.0), (6_000, 0.01), (7_000, 0.01)])
        (t,) = replay(busy, self.CONFIRM).closed
        self.assertGreaterEqual(t.entry_t, 1_005_000)                            # after the 2 SOL buy lifted the price
        self.assertGreater(t.entry_mcap, 27.96 * 1.1)

    def test_confirm_needs_net_buying_not_just_a_higher_price(self):
        x = Sim("M1", "C", 1_000_000)
        ev = [x.create(), x.buy(1_000_500, 3.0), x.scan(1_030_000), x.buy(1_035_000, 0.01)]  # the pump is over 15 s old when the token clears the scan
        strat = replace(self.CONFIRM, confirm_min_flow_sol=0.5, confirm_window_s=60.0)
        r = replay(ev, strat)
        self.assertEqual(r.closed, [])   # price is well above launch, but nobody has bought lately: no confirmation
        fresh = [x2 for x2 in [Sim("M2", "C", 2_000_000)]]
        y = fresh[0]
        ev2 = [y.create(), y.scan(2_000_100), y.buy(2_000_500, 3.0), y.buy(2_001_000, 0.01)]  # the same pump while the token is being watched
        self.assertEqual(len(replay(ev2, strat).closed), 1)

    def test_watch_expires_even_when_no_trades_arrive_for_that_token(self):
        ev = self.stream([(500, 0.01)])
        other = Sim("M2", "C2", 1_100_000)
        r = replay(ev + [other.create()], self.CONFIRM)   # an unrelated launch 100 s later moves the clock
        self.assertEqual(r.skipped["no_confirmation"], 1)

    def test_stopped_bot_does_not_start_watching(self):
        eng = PaperEngine(self.CONFIRM)
        eng.enabled = False
        for ev in self.stream([(500, 2.0)]):
            eng.on_event(ev)
        self.assertEqual((eng.watch, eng.skipped["bot_stopped"]), ({}, 1))
