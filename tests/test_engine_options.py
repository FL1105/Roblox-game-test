"""The bot-configuration options: trailing stop, slippage, liquidity floor, blacklist, size modes."""

import unittest
from dataclasses import replace

from sniper.paper.engine import PaperEngine, Strategy, replay
from sniper.pump.events import NewToken

from .sim import Sim

S = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1)


def run(events, strategy, start_sol=1.0):
    return replay(events, strategy, start_sol)


class TrailingStop(unittest.TestCase):
    def pump_then_fade(self, sim):
        # enter at launch price, pump +60%ish, then give back some of it
        return [
            sim.create(), sim.scan(1100), sim.buy(1200, 0.2),
            sim.buy(1300, 6.0),                      # big pump (peak)
            sim.sell(1400, 5.0e7, "OTHER"),          # partial fade
            sim.buy(1500, 0.01),
        ]

    def test_locks_in_profit_after_a_drop_from_the_peak(self):
        strat = replace(S, take_profit_pct=1000, stop_loss_pct=99, trailing_stop_pct=10)
        (t,) = run(self.pump_then_fade(Sim()), strat).closed
        self.assertEqual(t.reason, "trailing_stop")
        self.assertGreater(t.ret_pct, 0)

    def test_off_by_default(self):
        strat = replace(S, take_profit_pct=1000, stop_loss_pct=99)
        (t,) = run(self.pump_then_fade(Sim()), strat).closed
        self.assertEqual(t.reason, "end_of_data")

    def test_inactive_until_the_position_has_been_in_profit(self):
        sim = Sim()
        events = [sim.create(), sim.buy(1050, 8.0), sim.scan(1100), sim.buy(1200, 0.01), sim.sell(1300, 1.5e8, "X"), sim.buy(1400, 0.01)]
        (t,) = run(events, replace(S, take_profit_pct=1000, stop_loss_pct=99, trailing_stop_pct=5)).closed
        self.assertNotEqual(t.reason, "trailing_stop")  # never in profit, so the trail never armed

    def test_take_profit_still_wins_when_both_trigger(self):
        strat = replace(S, take_profit_pct=30, trailing_stop_pct=10)
        (t,) = run([Sim().create()] and self.pump_then_fade(Sim()), strat).closed
        self.assertEqual(t.reason, "take_profit")


class Slippage(unittest.TestCase):
    def events(self):
        sim = Sim()
        return [sim.create(), sim.scan(1100), sim.buy(1300, 1.0), sim.buy(1500, 5.0), sim.buy(1700, 0.01)]

    def test_entry_aborted_when_price_runs_away_during_the_delay(self):
        r = run(self.events(), replace(S, latency_ms=500, slippage_pct=5))
        self.assertEqual((r.closed, r.skipped["slippage"]), ([], 1))

    def test_entry_allowed_when_tolerance_is_wide_enough(self):
        r = run(self.events(), replace(S, latency_ms=500, slippage_pct=100))
        self.assertEqual(len(r.closed), 1)

    def test_no_delay_means_no_slippage(self):
        r = run(self.events(), replace(S, latency_ms=0, slippage_pct=0.01))
        self.assertEqual(r.skipped["slippage"], 0)


class LiquidityFloor(unittest.TestCase):
    def test_skips_tokens_with_too_little_sol_raised(self):
        sim = Sim()
        events = [sim.create(), sim.scan(1100), sim.buy(1200, 0.2)]
        r = run(events, replace(S, min_liquidity_sol=5))
        self.assertEqual((r.closed, r.skipped["low_liquidity"]), ([], 1))

    def test_allows_tokens_that_have_raised_enough(self):
        sim = Sim()
        events = [sim.create(), sim.buy(1050, 6.0), sim.scan(1100), sim.buy(1200, 0.2)]
        self.assertEqual(len(run(events, replace(S, min_liquidity_sol=5)).closed), 1)


class Blacklist(unittest.TestCase):
    def events(self, creator="BAD"):
        sim = Sim("M1", creator)
        return [sim.create(), sim.scan(1100), sim.buy(1200, 0.2)]

    def test_listed_creator_is_skipped(self):
        r = run(self.events(), replace(S, blacklist=frozenset({"BAD"})))
        self.assertEqual((r.closed, r.skipped["blacklisted"]), ([], 1))

    def test_toggle_off_ignores_the_list(self):
        r = run(self.events(), replace(S, blacklist=frozenset({"BAD"}), use_blacklist=False))
        self.assertEqual(len(r.closed), 1)

    def test_a_creator_seen_dumping_is_blocked_on_their_next_launch(self):
        first = Sim("M1", "RUGGER")
        launch = first.create(dev_sol=2.0)
        dump = [first.sell(1300, launch.dev_tokens, "RUGGER")]
        second = Sim("M2", "RUGGER", t=5000)
        events = [launch, *dump, second.create(), second.scan(5100), second.buy(5200, 0.2)]
        r = run(events, S)
        self.assertEqual(r.skipped["blacklisted"], 1)
        self.assertEqual(r.closed, [])

    def test_small_creator_sale_is_not_treated_as_a_rug(self):
        first = Sim("M1", "SELLER")
        launch = first.create(dev_sol=2.0)
        trickle = first.sell(1300, launch.dev_tokens * 0.1, "SELLER")
        second = Sim("M2", "SELLER", t=5000)
        r = run([launch, trickle, second.create(), second.scan(5100), second.buy(5200, 0.2)], S)
        self.assertEqual(r.skipped["blacklisted"], 0)


class SizeModes(unittest.TestCase):
    def sizes(self, strategy, n=12, start_sol=1.0):
        events = []
        for i in range(n):
            sim = Sim(f"M{i}", f"C{i}", t=1000 + i * 100_000)
            t0 = 1000 + i * 100_000
            events += [sim.create(), sim.scan(t0 + 100, "PASS"), sim.buy(t0 + 200, 0.2)]
        return [t.sol_in for t in run(events, replace(strategy, max_open=1), start_sol).closed]

    def test_random_mode_stays_in_range_and_varies(self):
        sizes = self.sizes(replace(S, size_mode="random", size_caution_sol=0.05, size_sol=0.2))
        self.assertTrue(all(0.05 <= x <= 0.2 for x in sizes))
        self.assertGreater(len(set(round(x, 6) for x in sizes)), 3)

    def test_random_mode_is_reproducible(self):
        strat = replace(S, size_mode="random", size_caution_sol=0.05, size_sol=0.2)
        self.assertEqual(self.sizes(strat), self.sizes(strat))

    def test_fixed_mode_always_uses_the_maximum(self):
        self.assertEqual(set(self.sizes(replace(S, size_mode="fixed", size_caution_sol=0.05, size_sol=0.2))), {0.2})

    def test_verdict_mode_uses_the_maximum_for_pass(self):
        self.assertEqual(set(self.sizes(replace(S, size_mode="verdict", size_caution_sol=0.05, size_sol=0.2))), {0.2})

    def test_auto_compound_scales_size_with_equity(self):
        eng = PaperEngine(replace(S, size_mode="fixed", size_sol=0.1, auto_compound=True), start_sol=1.0)
        eng.cash = 2.0  # equity doubled
        self.assertAlmostEqual(eng._size("M", 2.0), 0.2)
        eng.cash = 0.5  # equity halved
        self.assertAlmostEqual(eng._size("M", 0.5), 0.05)

    def test_auto_compound_off_keeps_size_flat(self):
        eng = PaperEngine(replace(S, size_mode="fixed", size_sol=0.1, auto_compound=False), start_sol=1.0)
        self.assertAlmostEqual(eng._size("M", 5.0), 0.1)


if __name__ == "__main__":
    unittest.main()
