import math
import random
import unittest

from kalshi_btc15.api import Market
from kalshi_btc15.stack import (HOLDOUT_RANGE, compare, day_ts, evaluate, features, fit_logistic, fit_stack, in_dev, invert,
                                make_strategy, render_stack, sample_range)
from kalshi_btc15.strategies import Config, run

from .test_kalshi_btc15 import sample, synthetic_worlds

CFG = Config(contracts=10)


class Algebra(unittest.TestCase):
    def test_inverse(self):
        a = [[4.0, 1.0, 0.5], [1.0, 3.0, 0.2], [0.5, 0.2, 2.0]]
        inv = invert(a)
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(sum(a[i][k] * inv[k][j] for k in range(3)), 1.0 if i == j else 0.0, places=9)

    def test_singular_matrix_is_reported(self):
        with self.assertRaises(ValueError):
            invert([[1.0, 2.0], [2.0, 4.0]])

    def test_logistic_regression_recovers_known_coefficients(self):
        rng = random.Random(3)
        x = [[1.0, rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(8000)]
        truth = [-0.3, 1.2, 0.0]
        y = [int(rng.random() < 1 / (1 + math.exp(-sum(b * v for b, v in zip(truth, row))))) for row in x]
        beta, se = fit_logistic(x, y, [0.0, 0.0, 0.0])
        for got, want, err in zip(beta, truth, se):
            self.assertLess(abs(got - want), 4 * err)
            self.assertGreater(err, 0)

    def test_ridge_shrinks_towards_zero(self):
        rng = random.Random(4)
        x = [[1.0, rng.gauss(0, 1)] for _ in range(500)]
        y = [int(rng.random() < 1 / (1 + math.exp(-1.5 * row[1]))) for row in x]
        free, _ = fit_logistic(x, y, [0.0, 0.0])
        shrunk, _ = fit_logistic(x, y, [0.0, 200.0])
        self.assertLess(abs(shrunk[1]), abs(free[1]))


class Split(unittest.TestCase):
    def test_dev_and_holdout_do_not_overlap(self):
        self.assertTrue(in_dev(day_ts("2026-01-03")))
        self.assertFalse(in_dev(day_ts("2026-01-04")))
        self.assertFalse(in_dev(day_ts("2026-08-20")))
        self.assertTrue(in_dev(day_ts("2026-08-21")))
        lo, hi = HOLDOUT_RANGE
        self.assertFalse(in_dev(lo) or in_dev(hi - 1))

    def test_sampling_is_positional_and_ignores_input_order(self):
        markets = [Market(f"T{i}", 100 + i, True, None) for i in range(20)]
        picked = sample_range(list(reversed(markets)), 100, 120, 4)
        self.assertEqual([m.ticker for m in picked], ["T0", "T4", "T8", "T12", "T16"])
        self.assertEqual(sample_range(markets, 105, 120, 4)[0].ticker, "T5")


class Features(unittest.TestCase):
    def test_features_need_every_input(self):
        self.assertEqual(len(features(sample(), CFG)), 5)
        for field in ("spot_open", "spot_prev", "bid_prev", "sigma"):
            self.assertIsNone(features(sample(**{field: None}), CFG), field)

    def test_features_point_the_right_way(self):
        up, down = features(sample(spot_entry=100.3, ask=80, bid=78), CFG), features(sample(spot_entry=99.7, ask=80, bid=78), CFG)
        self.assertGreater(up[1], 0)
        self.assertLess(down[1], 0)
        self.assertAlmostEqual(up[0], down[0])
        self.assertGreater(features(sample(last=79.0, ask=80, bid=60), CFG)[3], 0)   # last trade above the mid
        self.assertEqual(features(sample(last=None), CFG)[3], 0.0)


class Worlds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fair, cls.shrunk = synthetic_worlds(n=3000)

    def split(self, world):
        half = len(world) // 2
        return world[:half], world[half:]

    def test_when_the_market_is_exact_the_stack_adds_nothing(self):
        dev, hold = self.split(self.fair)
        r = evaluate(dev, hold, CFG)
        self.assertEqual(r.forecast_verdict, "no information added")
        self.assertTrue(r.trades_verdict.startswith("no "), r.trades_verdict)

    def test_when_the_market_underreacts_the_stack_finds_it(self):
        dev, hold = self.split(self.shrunk)
        r = evaluate(dev, hold, CFG)
        self.assertTrue(r.forecast_verdict.startswith("PASS"), r.forecast_verdict)
        stacked = r.comparisons[-1]
        self.assertLess(stacked.delta, -0.01)
        self.assertGreater(r.model.beta[1], 1.0)   # the market is under-confident, so its logit gets stretched
        trades = run(make_strategy(r.model), hold, CFG)
        self.assertGreater(len(trades), 300)
        self.assertGreater(sum(t.pnl for t in trades) / len(trades), 2.0)

    def test_report_renders_with_and_without_a_holdout(self):
        dev, hold = self.split(self.shrunk)
        r = evaluate(dev, hold, CFG)
        text = render_stack(r, dev, hold, CFG, {"holdout_sampled": 10, "holdout_downloaded": 10})
        for needle in ("H_F", "H_T", "gap z-score", "stack_edge by month", "Threshold sensitivity"):
            self.assertIn(needle, text)
        empty = evaluate(dev, [], CFG)
        self.assertIn("No holdout markets", render_stack(empty, dev, [], CFG, {"holdout_sampled": 10, "holdout_downloaded": 0}))

    def test_the_strategy_does_not_trade_when_it_agrees_with_the_market(self):
        dev, _ = self.split(self.fair)
        model, n = fit_stack(dev, CFG)
        self.assertGreater(n, 1000)
        self.assertLess(len(run(make_strategy(model), dev, CFG)), 0.05 * len(dev))


if __name__ == "__main__":
    unittest.main()
