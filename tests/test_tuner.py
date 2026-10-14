"""Auto-tuning with clones: the plumbing, the statistics, and a market where the right answer is known."""

import math
import unittest
from dataclasses import replace

from sniper.paper.engine import PaperEngine, Strategy
from unittest import mock

from sniper.paper import tuner as tuner_module
from sniper.paper.tuner import (BY_KEY, CAUTION, DEFAULT_PARAMS, MIN_DIFFERING, TUNABLES, Tuner, TunerConfig, blend_changes,
                                alpha_for, clone_passes, launch_outcomes, mulberry32, paired_gate, parse_tuner, resample_counts, snap)
from sniper.pump import synth
from sniper.pump.events import NewToken
from sniper.pump.synth import Sim


def pump_and_dump(n, pump=8.0, gap=10_000, start=1000):
    """Every token pumps to about +55% for an instant, then everyone who bought sells. Taking profit at +50% wins; waiting loses."""
    events = []
    for i in range(n):
        t0 = start + i * gap
        sim = Sim(f"M{i:03d}", f"C{i:03d}", t0)
        events += [sim.create(), sim.scan(t0 + 100), sim.buy(t0 + 300, 0.01, "B1")]
        pumped = sim.buy(t0 + 500, pump, "B2")
        events += [pumped, sim.sell(t0 + 700, pumped.tokens, "B2")]
    return events


QUICK = Strategy(latency_ms=0, tx_cost_sol=0.0, max_open=50, max_hold_s=30, size_sol=0.1)


def run(tuner, engine, events, apply=True):
    """Feed events to the real bot and the tuner the way TerminalState does. Returns the verdicts."""
    verdicts = []
    for ev in events:
        engine.on_event(ev)
        v = tuner.feed(ev, engine)
        if v:
            verdicts.append(v)
            if apply and v["changes"]:
                engine.s = replace(engine.s, **{BY_KEY[k].field: (int(new) if BY_KEY[k].integer else new) for k, (old, new) in v["changes"].items()})
    return verdicts


class Rng(unittest.TestCase):
    def test_it_is_deterministic_in_range_and_roughly_uniform(self):
        a, b = mulberry32(5), mulberry32(5)
        xs = [a() for _ in range(5000)]
        self.assertEqual(xs[:50], [b() for _ in range(50)])
        self.assertTrue(all(0 <= x < 1 for x in xs))
        self.assertAlmostEqual(sum(xs) / len(xs), 0.5, delta=0.02)
        self.assertNotEqual(xs[:5], [mulberry32(6)() for _ in range(5)])

    def test_snap_rounds_half_up_to_the_step_without_float_dust(self):
        self.assertEqual([snap(0.7000000000000001, 0.1), snap(32.74, 0.5), snap(32.75, 0.5), snap(7.5, 1), snap(7.49, 1), snap(1234, 5), snap(0, 0.5)],
                         [0.7, 32.5, 33.0, 8.0, 7.0, 1235.0, 0.0])


class Settings(unittest.TestCase):
    def test_valid_partial_updates(self):
        cur = TunerConfig()
        new = parse_tuner({"enabled": True, "clones": 4, "caution": "strict", "blend": 0.25, "params": ["stop_loss", "take_profit"]}, cur)
        self.assertEqual((new.enabled, new.clones, new.caution, new.blend, new.round_launches), (True, 4, "strict", 0.25, 100))
        self.assertEqual(new.params, ("take_profit", "stop_loss"))  # canonical order, whatever the caller sent

    def test_bad_values_are_refused_with_a_message(self):
        cur = TunerConfig()
        for data, text in [({"enabled": "yes"}, "on or off"), ({"clones": 1}, "clones"), ({"clones": 9}, "clones"), ({"clones": 2.5}, "clones"),
                           ({"clones": True}, "clones"), ({"round_launches": 10}, "Launches"), ({"round_launches": float("nan")}, "Launches"),
                           ({"caution": "yolo"}, "Caution"), ({"blend": 0}, "Step"), ({"blend": 1.5}, "Step"), ({"blend": True}, "Step"),
                           ({"caution": ["x"]}, "Caution"), ({"caution": 5}, "Caution"), ({"caution": None}, "Caution"), ({"params": [["take_profit"]]}, "params"),
                           ({"params": [5]}, "params"), ({"params": [None]}, "params"), ({"params": ["nope"]}, "params"), ({"params": ["stop_loss", "stop_loss"]}, "params"), ({"params": "stop_loss"}, "params"),
                           ({"surprise": 1}, "unknown")]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError) as cm:
                    parse_tuner(data, cur)
                self.assertIn(text, str(cm.exception))

    def test_limits_sit_inside_what_the_settings_screen_accepts(self):
        from sniper.terminal.state import default_strategy, parse_settings
        for t in TUNABLES:
            for value in (t.lo, t.hi):
                with self.subTest(setting=t.key, value=value):
                    parse_settings({t.key: value}, default_strategy())


class Planning(unittest.TestCase):
    def tuner(self, **kw):
        return Tuner(TunerConfig(enabled=True, **kw))

    def test_nudges_come_in_opposite_pairs_on_the_same_settings(self):
        t, s = self.tuner(clones=6), Strategy(take_profit_pct=30, stop_loss_pct=20, trailing_stop_pct=10, max_hold_s=300, min_liquidity_sol=2, max_dev_buy_pct=15)
        plans = t._plan(s, t.active_tunables(s))
        self.assertEqual(len(plans), 6)
        for up, down in zip(plans[0::2], plans[1::2]):
            for key in set(up) | set(down):
                cur = float(getattr(s, BY_KEY[key].field))
                if key in up and key in down:
                    self.assertGreater(up[key], cur)
                    self.assertLess(down[key], cur)
        self.assertTrue(all(plans[i] or plans[i + 1] for i in range(0, 6, 2)))  # every pair tries something

    def test_clones_can_be_odd_in_number(self):
        t, s = self.tuner(clones=3), Strategy()
        self.assertEqual(len(t._plan(s, t.active_tunables(s))), 3)

    def test_everything_stays_inside_the_limits_and_on_the_grid(self):
        t = self.tuner(clones=8)
        for s in (Strategy(take_profit_pct=8, stop_loss_pct=5, min_trades=0, trailing_stop_pct=0),
                  Strategy(take_profit_pct=150, stop_loss_pct=60, min_trades=30, trailing_stop_pct=40, max_hold_s=1800)):
            for _ in range(50):
                for plan in t._plan(s, t.active_tunables(s)):
                    for key, value in plan.items():
                        spec = BY_KEY[key]
                        cur = float(getattr(s, spec.field))
                        self.assertGreaterEqual(value, min(spec.lo, cur))
                        self.assertLessEqual(value, max(spec.hi, cur))
                        self.assertNotEqual(value, cur)
                        self.assertAlmostEqual(value / spec.precision, round(value / spec.precision), places=6)

    def test_a_setting_already_outside_the_limits_is_nudged_from_where_it_is(self):
        t, s = self.tuner(clones=6, params=("take_profit",)), Strategy(take_profit_pct=300)
        values = {p["take_profit"] for p in t._plan(s, t.active_tunables(s)) if p}
        self.assertTrue(values)
        self.assertTrue(all(200 < v < 400 for v in values), values)

    def test_settings_that_do_not_matter_for_this_strategy_are_left_alone(self):
        t = self.tuner()
        keys = lambda s: {x.key for x in t.active_tunables(s)}
        self.assertIn("take_profit", keys(Strategy()))
        self.assertNotIn("take_profit", keys(Strategy(ladder=((30.0, 50.0),))))
        self.assertIn("take_profit", keys(Strategy(exit_mode="smart", ladder=((30.0, 50.0),))))
        self.assertNotIn("take_profit", keys(Strategy(exit_mode="model")))
        self.assertNotIn("trailing_stop_pct", keys(Strategy(exit_mode="smart")))
        self.assertNotIn("confirm_gain_pct", keys(Strategy()))
        self.assertIn("confirm_gain_pct", keys(Strategy(entry_mode="confirm")))
        self.assertNotIn("max_entry_gain_pct", keys(Strategy()))
        self.assertIn("max_entry_gain_pct", keys(Strategy(max_entry_gain_pct=80)))

    def test_only_the_chosen_settings_are_tuned(self):
        t = self.tuner(params=("stop_loss",))
        for plan in t._plan(Strategy(), t.active_tunables(Strategy())):
            self.assertLessEqual(set(plan), {"stop_loss"})

    def test_nothing_to_tune_means_clones_identical_to_the_bot(self):
        t = self.tuner(params=())
        self.assertEqual(t._plan(Strategy(), []), [{}] * 6)


class Statistics(unittest.TestCase):
    ALPHA = (1 - CAUTION["balanced"]) / 6

    def gate(self, diffs, alpha=None, seed=1):
        return paired_gate(diffs, self.ALPHA if alpha is None else alpha, mulberry32(seed))

    def test_a_steady_gain_is_clearly_better(self):
        (mean, lcb, differing), = self.gate([[10.0] * 100])
        self.assertEqual((mean, differing), (10.0, 100))
        self.assertAlmostEqual(lcb, 10.0)

    def test_no_difference_is_no_evidence(self):
        (mean, lcb, differing), = self.gate([[0.0] * 100])
        self.assertEqual((mean, lcb, differing), (0.0, 0.0, 0))

    def test_noise_around_zero_has_a_negative_lower_bound(self):
        rng = mulberry32(3)
        d = [(rng() - 0.5) * 40 for _ in range(100)]
        (mean, lcb, _), = self.gate([d])
        self.assertLess(lcb, 0)
        self.assertLess(lcb, mean)

    def test_one_huge_lucky_launch_is_not_enough_launches_to_count(self):
        d = [0.0] * 99 + [500.0]
        (mean, lcb, differing), = self.gate([d])
        self.assertGreater(mean, 0)
        self.assertLess(differing, MIN_DIFFERING)

    def test_a_few_big_wins_among_many_ties_has_a_wide_interval(self):
        d = [0.0] * 90 + [30.0] * 6 + [-30.0] * 4
        (mean, lcb, differing), = self.gate([d])
        self.assertGreater(mean, 0)
        self.assertLess(lcb, 0)

    def test_the_same_resamples_are_used_for_every_clone(self):
        a, b = [3.0] * 50 + [0.0] * 50, [3.0] * 50 + [0.0] * 50
        (m1, l1, _), (m2, l2, _) = self.gate([a, b])
        self.assertEqual((m1, l1), (m2, l2))

    def test_empty_round_is_harmless(self):
        self.assertEqual(self.gate([[]]), [(0.0, 0.0, 0)])
        self.assertEqual(self.gate([]), [])

    def promotions(self, caution, trials=80, n_clones=6, effect=0.0, seed=11):
        """How often the best of `n_clones` noise-only clones would be promoted (the rule used by the tuner)."""
        rng, hits = mulberry32(seed), 0
        alpha = (1 - CAUTION[caution]) / n_clones
        counts = resample_counts(100, mulberry32(seed + 1))  # resampling weights don't depend on the data, so one set can serve every trial
        for _ in range(trials):
            diffs = []
            for _c in range(n_clones):
                # a sparse, fat-tailed difference: most launches tie, a few differ by a lot
                diffs.append([(rng() - 0.5 + effect) * 80 if rng() < 0.25 else 0.0 for _ in range(100)])
            gate = paired_gate(diffs, alpha, rng, counts)
            if any(m > 0 and k >= MIN_DIFFERING and lcb > 0 for m, lcb, k in gate):
                hits += 1
        return hits / trials

    def test_pure_noise_is_rarely_promoted_and_stricter_settings_promote_less(self):
        rates = {c: self.promotions(c) for c in ("eager", "balanced", "careful", "strict")}
        self.assertLessEqual(rates["balanced"], 0.15, rates)  # nominal 10%
        self.assertLessEqual(rates["careful"], 0.10, rates)   # nominal 5%
        self.assertLessEqual(rates["strict"], 0.04, rates)    # nominal 1%
        self.assertLessEqual(rates["eager"], 0.30, rates)     # nominal 20%
        self.assertGreaterEqual(rates["eager"], rates["balanced"])
        self.assertGreaterEqual(rates["balanced"], rates["strict"])

    def test_a_real_edge_is_usually_found(self):
        self.assertGreaterEqual(self.promotions("balanced", trials=60, effect=0.35), 0.8)


class BlendingSettings(unittest.TestCase):
    def tunables(self, *keys):
        return [BY_KEY[k] for k in keys]

    def test_moves_part_of_the_way_toward_the_gain_weighted_average(self):
        s = Strategy(take_profit_pct=30)
        out = blend_changes(s, self.tunables("take_profit"), [({"take_profit": 40.0}, 3.0), ({"take_profit": 32.0}, 1.0)], 0.5)
        self.assertEqual(out, {"take_profit": [30.0, 34.0]})  # weighted average 38 (equal weights would give 36), halfway

    def test_a_setting_a_winner_left_alone_counts_as_staying_put(self):
        s = Strategy(take_profit_pct=30, stop_loss_pct=20)
        out = blend_changes(s, self.tunables("take_profit", "stop_loss"), [({"take_profit": 40.0}, 1.0), ({"stop_loss": 10.0}, 1.0)], 1.0)
        self.assertEqual(out, {"take_profit": [30.0, 35.0], "stop_loss": [20.0, 15.0]})

    def test_nothing_changes_without_winners_or_when_the_move_rounds_away(self):
        s = Strategy(take_profit_pct=30)
        self.assertEqual(blend_changes(s, self.tunables("take_profit"), [], 0.5), {})
        self.assertEqual(blend_changes(s, self.tunables("take_profit"), [({"take_profit": 30.5}, 1.0)], 0.4), {})

    def test_whole_number_settings_and_limits(self):
        s = Strategy(min_trades=2)
        self.assertEqual(blend_changes(s, self.tunables("min_trades"), [({"min_trades": 5.0}, 1.0)], 1.0), {"min_trades": [2.0, 5.0]})
        s = Strategy(stop_loss_pct=58)
        self.assertEqual(blend_changes(s, self.tunables("stop_loss"), [({"stop_loss": 90.0}, 1.0)], 1.0), {"stop_loss": [58.0, 60.0]})  # held at the limit


class Rounds(unittest.TestCase):
    def test_a_round_ends_after_the_set_number_of_launches_and_a_new_one_starts(self):
        market = synth.generate(5, 70, 1_000_000)
        tuner = Tuner(TunerConfig(enabled=True, clones=4, round_launches=20))
        engine = PaperEngine(Strategy(latency_ms=300), 1.0)
        verdicts = run(tuner, engine, market, apply=False)
        self.assertEqual(len(verdicts), 3)  # 70 launches: three rounds of 20, plus the start of the next
        self.assertEqual([v["round"] for v in verdicts], [1, 2, 3])
        self.assertTrue(all(v["launches"] == 20 and len(v["clones"]) == 4 for v in verdicts))
        self.assertEqual([h["round"] for h in tuner.history], [3, 2, 1])  # newest first

    def test_disabled_or_paused_does_nothing(self):
        market = synth.generate(5, 30, 1_000_000)
        for tuner in (Tuner(TunerConfig(enabled=False, round_launches=20)), Tuner(TunerConfig(enabled=True, round_launches=20))):
            tuner.paused = "pause" if tuner.cfg.enabled else None
            engine = PaperEngine(Strategy(), 1.0)
            self.assertEqual(run(tuner, engine, market), [])
            self.assertEqual(tuner.clones, [])

    def test_it_is_deterministic(self):
        market = synth.generate(9, 120, 1_000_000)
        outcomes = []
        for _ in range(2):
            tuner = Tuner(TunerConfig(enabled=True, clones=6, round_launches=30, caution="eager"))
            run(tuner, PaperEngine(Strategy(latency_ms=300), 1.0), market)
            outcomes.append(tuner.history)
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertTrue(outcomes[0])

    def test_clones_are_judged_only_on_launches_after_they_were_made(self):
        market = pump_and_dump(60)
        engine = PaperEngine(QUICK, 1.0)
        tuner = Tuner(TunerConfig(enabled=True, clones=4, round_launches=20, params=("take_profit",)))
        for ev in market[: len(market) // 2]:
            engine.on_event(ev)  # the bot has traded for a while before the tuner is switched on
        before = len(engine.closed)
        self.assertGreater(before, 5)
        run(tuner, engine, market[len(market) // 2:], apply=False)
        late = {m for m in (e.mint for e in market[len(market) // 2:] if isinstance(e, NewToken))}
        self.assertTrue(tuner.history)
        for verdict in tuner.history:
            self.assertEqual(verdict["launches"], 20)
        # outcomes are computed for the round's own launches only, never for older ones the clones inherited
        first = tuner.history[-1]
        self.assertLessEqual(first["bot_trades"], 20)
        self.assertTrue(set(launch_outcomes(engine, sorted(late))) <= late)

    def test_the_clone_that_ties_the_bot_is_never_promoted(self):
        market = synth.generate(2, 80, 1_000_000)
        tuner = Tuner(TunerConfig(enabled=True, clones=4, round_launches=20, params=()))
        verdicts = run(tuner, PaperEngine(Strategy(latency_ms=300), 1.0), market)
        self.assertTrue(verdicts)
        for v in verdicts:
            self.assertEqual((v["decision"], v["changes"]), ("kept", {}))
            self.assertFalse(any(c["passed"] for c in v["clones"]))


class FindsTheRightAnswer(unittest.TestCase):
    """A market where taking profit at about +50% clearly beats the bot's 60% target, so the tuner has something real to find."""

    def setUp(self):
        sim = Sim("X", "Y", 0)
        sim.buy(1, 0.01, "B1")
        probe = PaperEngine(replace(QUICK, take_profit_pct=1000, stop_loss_pct=99), 1.0)
        for ev in pump_and_dump(1)[:4]:
            probe.on_event(ev)
        from sniper.pump.curve import quote_sell
        pos = next(iter(probe.positions.values()))
        self.peak = (quote_sell(probe.curve[pos.mint], pos.tokens, probe.s.fee_bps) / pos.sol_in - 1) * 100
        self.assertTrue(50 < self.peak < 58, self.peak)  # the construction of the market itself

    def test_a_too_ambitious_take_profit_is_walked_down_until_it_pays(self):
        engine = PaperEngine(replace(QUICK, take_profit_pct=60), 1.0)
        tuner = Tuner(TunerConfig(enabled=True, clones=6, round_launches=40, caution="balanced", blend=0.5, params=("take_profit",)))
        verdicts = run(tuner, engine, pump_and_dump(280))
        changed = [v for v in verdicts if v["decision"] == "changed"]
        self.assertGreaterEqual(len(changed), 2, [v["reason"] for v in verdicts])
        tps = [engine.s.take_profit_pct]
        self.assertLess(engine.s.take_profit_pct, self.peak)       # now low enough to be hit
        self.assertGreater(engine.s.take_profit_pct, 20)           # and not driven to nonsense
        first = changed[0]
        self.assertTrue(any(c["passed"] for c in first["clones"]))
        self.assertTrue(all(c["changes"]["take_profit"] < 60 for c in first["clones"] if c["passed"]))
        self.assertGreater(first["clones"][0]["lcb"] if first["clones"][0]["passed"] else min(c["lcb"] for c in first["clones"] if c["passed"]), 0)
        for step in changed:
            (old, new), = step["changes"].values()
            self.assertLess(new, old)
            self.assertLess(abs(new - old), 0.5 * old)  # partial steps, never a leap
        # once the target is reachable there is nothing left to improve: the last rounds keep their settings
        self.assertEqual(verdicts[-1]["decision"], "kept")
        # and the real bot now makes money on this market
        late = engine.closed[-30:]
        self.assertGreater(sum(c.ret_pct for c in late) / len(late), 20)

    def test_a_good_setting_is_left_alone(self):
        engine = PaperEngine(replace(QUICK, take_profit_pct=45), 1.0)
        tuner = Tuner(TunerConfig(enabled=True, clones=6, round_launches=40, params=("take_profit",)))
        verdicts = run(tuner, engine, pump_and_dump(200))
        self.assertTrue(verdicts)
        self.assertTrue(all(v["decision"] == "kept" for v in verdicts), [v["reason"] for v in verdicts])
        self.assertEqual(engine.s.take_profit_pct, 45)


class Rule(unittest.TestCase):
    def test_a_clone_must_clear_every_bar(self):
        base = dict(changes={"take_profit": 40.0}, mean=2.0, lcb=0.5, differing=MIN_DIFFERING)
        self.assertTrue(clone_passes(**base))
        for name, override in [("no settings changed", {"changes": {}}), ("no average gain", {"mean": 0.0}), ("average loss", {"mean": -1.0}),
                               ("too few launches", {"differing": MIN_DIFFERING - 1}), ("bound at zero", {"lcb": 0.0}), ("bound below zero", {"lcb": -0.1})]:
            with self.subTest(name):
                self.assertFalse(clone_passes(**{**base, **override}))

    def test_allowed_luck_is_shared_across_clones(self):
        self.assertAlmostEqual(alpha_for("balanced", 6), 0.1 / 6)
        self.assertAlmostEqual(alpha_for("strict", 2), 0.005)
        self.assertLess(alpha_for("balanced", 8), alpha_for("balanced", 4))


class Judging(unittest.TestCase):
    """The round-end logic, with the statistics replaced by scripted numbers so each rule can be seen on its own."""

    def judge(self, gate, blend_override=None, **cfg):
        config = dict(enabled=True, clones=4, round_launches=20, params=("take_profit", "stop_loss"), caution="balanced")
        config.update(cfg)
        tuner = Tuner(TunerConfig(**config))
        engine = PaperEngine(replace(QUICK, take_profit_pct=60), 1.0)
        calls = []

        def fake_gate(diffs, alpha, rng, counts=None):
            calls.append(alpha)
            return gate(len(diffs))

        patches = [mock.patch.object(tuner_module, "paired_gate", fake_gate)]
        if blend_override is not None:
            patches.append(mock.patch.object(tuner_module, "blend_changes", blend_override))
        with patches[0]:
            if len(patches) == 2:
                with patches[1]:
                    verdicts = run(tuner, engine, pump_and_dump(30), apply=False)
            else:
                verdicts = run(tuner, engine, pump_and_dump(30), apply=False)
        return verdicts[0], calls, tuner

    def test_scripted_winner_is_promoted_and_the_others_are_not(self):
        verdict, calls, _ = self.judge(lambda n: [(3.0, 1.0, 9)] + [(3.0, -1.0, 9)] * (n - 1))
        self.assertEqual([c["passed"] for c in verdict["clones"]], [True, False, False, False])
        self.assertEqual(verdict["decision"], "changed")
        self.assertIn("1 of 4 clones beat the bot with 90% confidence", verdict["reason"])
        self.assertTrue(verdict["changes"])
        self.assertEqual(calls, [alpha_for("balanced", 4)])
        self.assertNotEqual(verdict["bot_mean"], 0.0)
        self.assertGreater(verdict["bot_trades"], 0)

    def test_nobody_clears_the_bar(self):
        verdict, _, _ = self.judge(lambda n: [(3.0, -1.0, 9)] * n, caution="strict")
        self.assertEqual((verdict["decision"], verdict["changes"]), ("kept", {}))
        self.assertEqual(verdict["reason"], "no clone beat the bot with 99% confidence")

    def test_a_win_too_small_to_apply_is_reported_as_kept(self):
        verdict, _, _ = self.judge(lambda n: [(3.0, 1.0, 9)] * n, blend_override=lambda *a: {})
        self.assertEqual((verdict["decision"], verdict["changes"], verdict["reason"]), ("kept", {}, "the winning move was too small to apply"))

    def test_clones_with_nothing_changed_can_never_win(self):
        verdict, _, _ = self.judge(lambda n: [(3.0, 1.0, 9)] * n, params=())
        self.assertFalse(any(c["passed"] for c in verdict["clones"]))
        self.assertEqual(verdict["decision"], "kept")


class OutcomesAndClones(unittest.TestCase):
    def test_a_position_still_open_is_counted_at_its_current_price(self):
        sim = Sim("M", "C", 1000)
        engine = PaperEngine(replace(QUICK, take_profit_pct=1000, stop_loss_pct=99), 1.0)
        for ev in [sim.create(), sim.scan(1100), sim.buy(1300, 0.01, "B1"), sim.buy(1500, 5.0, "B2")]:
            engine.on_event(ev)
        self.assertEqual(len(engine.positions), 1)
        out = launch_outcomes(engine, ["M"])
        self.assertGreater(out["M"], 25)
        self.assertEqual(launch_outcomes(engine, ["OTHER"]), {})

    def test_clones_get_whole_number_settings_as_whole_numbers(self):
        tuner = Tuner(TunerConfig(enabled=True, clones=4, round_launches=20, params=("min_trades",)))
        engine = PaperEngine(Strategy(min_trades=3), 1.0)
        run(tuner, engine, pump_and_dump(3), apply=False)
        self.assertTrue(tuner.clones)
        for c in tuner.clones:
            self.assertIsInstance(c.engine.s.min_trades, int)
        self.assertGreater(len({c.engine.s.min_trades for c in tuner.clones}), 1)

    def test_a_nudge_is_never_smaller_than_one_step_of_the_setting(self):
        t = Tuner(TunerConfig(enabled=True, clones=4, params=("max_dev_buy_pct",)))
        s = Strategy(max_dev_buy_pct=1.0)  # 25% of 1 is far below the 0.5 grid
        plans = t._plan(s, t.active_tunables(s))
        self.assertEqual({p["max_dev_buy_pct"] for p in plans if p}, {1.5})  # can't go below 1, so only the upward clone differs


class Snapshots(unittest.TestCase):
    def test_snapshot_describes_the_round_in_progress(self):
        engine = PaperEngine(replace(QUICK, take_profit_pct=60), 1.0)
        tuner = Tuner(TunerConfig(enabled=True, clones=4, round_launches=50, params=("take_profit", "stop_loss")))
        market = pump_and_dump(30)
        run(tuner, engine, market[:60], apply=False)
        snap_ = tuner.snapshot(engine)
        self.assertEqual((snap_["enabled"], snap_["round"], len(snap_["clones"])), (True, 1, 4))
        self.assertGreater(snap_["progress"], 5)
        self.assertEqual({p["key"] for p in snap_["params"]}, {t.key for t in TUNABLES})
        self.assertTrue(all(p["tuned"] for p in snap_["params"] if p["key"] in ("take_profit", "stop_loss")))
        self.assertFalse(any(p["tuned"] for p in snap_["params"] if p["key"] == "max_hold_s"))
        self.assertEqual(snap_["config"]["params"], ["take_profit", "stop_loss"])
        self.assertEqual(snap_["cautions"], {"eager": 80, "balanced": 90, "careful": 95, "strict": 99})
        for row in snap_["clones"]:
            self.assertEqual(set(row), {"id", "changes", "trades", "mean", "delta"})
        self.assertTrue(any(row["delta"] != 0 for row in snap_["clones"]))

    def test_snapshot_before_anything_runs(self):
        snap_ = Tuner().snapshot(PaperEngine(Strategy(), 1.0))
        self.assertEqual((snap_["enabled"], snap_["clones"], snap_["history"], snap_["progress"], snap_["bot_mean"]), (False, [], [], 0, 0.0))
        self.assertEqual(snap_["config"]["params"], list(DEFAULT_PARAMS))

    def test_history_is_capped(self):
        tuner = Tuner(TunerConfig(enabled=True, clones=2, round_launches=20, params=()))
        run(tuner, PaperEngine(QUICK, 1.0), pump_and_dump(20 * 40 + 5), apply=False)
        self.assertEqual(len(tuner.history), 30)
        self.assertEqual(tuner.history[0]["round"], 40)


if __name__ == "__main__":
    unittest.main()
