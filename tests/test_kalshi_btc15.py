import math
import random
import unittest
from collections import Counter

from kalshi_btc15.api import Candle, Market, candle_batches, cents, parse_candle, parse_market
from kalshi_btc15.data import MOM_MIN, Sample, build_samples
from kalshi_btc15.report import ALPHA, preregistered, render, verdict
from kalshi_btc15.spot import SpotSeries
from kalshi_btc15.stats import longest_run, max_drawdown, norm_cdf, summarize, wilson
from kalshi_btc15.strategies import (CONTROLS, PREREGISTERED, Config, Trade, effective_minutes, fee_cents, model_edge,
                                     model_p_up, run, take, trend_open)

CFG = Config(contracts=10)


def sample(**kw):
    base = dict(ticker="T", close_ts=1000, yes_won=True, bid=58.0, ask=60.0, bid_prev=50.0, ask_prev=52.0, tau_min=2.0,
                strike=100.0, spot_open=100.0, spot_prev=100.0, spot_entry=100.2, spot_end=100.3, sigma=0.0007)
    base.update(kw)
    return Sample(**base)


class Fee(unittest.TestCase):
    def test_rounds_up_on_the_whole_order(self):
        self.assertAlmostEqual(fee_cents(90, 10), 0.7)    # 0.07 * 10 * 0.09 = 6.3 cents -> 7 -> 0.7 per contract
        self.assertAlmostEqual(fee_cents(50, 1), 2.0)     # 1.75 cents -> 2
        self.assertAlmostEqual(fee_cents(50, 100), 1.75)  # large orders pay almost exactly the formula

    def test_multiplier_scales(self):
        self.assertLess(fee_cents(50, 100, 0.5), fee_cents(50, 100, 1.0))


class Model(unittest.TestCase):
    def test_effective_time_is_the_variance_of_a_trailing_average(self):
        # Var(mean of W over [a, tau]) = mean over pairs of min(u, v); compare with tau - 2L/3 on a fine grid.
        tau, length, n = 2.5, 1.0, 400
        grid = [tau - length + length * (i + 0.5) / n for i in range(n)]
        var = sum(min(u, v) for u in grid for v in grid) / (n * n)
        self.assertAlmostEqual(var, effective_minutes(tau, length), places=2)

    def test_probability_is_symmetric_and_monotone(self):
        up = model_p_up(sample(spot_entry=100.2), CFG)
        down = model_p_up(sample(spot_entry=100.0 * 100.0 / 100.2), CFG)
        self.assertAlmostEqual(up + down, 1.0, places=3)
        self.assertEqual(model_p_up(sample(spot_entry=100.0), CFG), 0.5)
        self.assertGreater(up, 0.9)
        self.assertGreater(model_p_up(sample(spot_entry=100.2, tau_min=1.5), CFG), up)

    def test_no_model_without_volatility_or_spot(self):
        self.assertIsNone(model_p_up(sample(sigma=None), CFG))
        self.assertIsNone(model_p_up(sample(spot_open=None), CFG))


class Fills(unittest.TestCase):
    def test_yes_is_bought_at_the_ask_and_no_at_100_minus_the_bid(self):
        y = take(sample(), "yes", CFG)
        n = take(sample(), "no", CFG)
        self.assertEqual((y.price, n.price), (60.0, 42.0))
        self.assertTrue(y.win)
        self.assertFalse(n.win)
        self.assertAlmostEqual(y.pnl, 100 - 60 - y.fee)
        self.assertAlmostEqual(n.pnl, -42 - n.fee)

    def test_untradable_prices_are_skipped(self):
        self.assertIsNone(take(sample(ask=100.0, bid=99.0), "yes", CFG))
        self.assertIsNone(take(sample(bid=0.0, ask=2.0), "no", CFG))

    def test_trend_rules_and_ties(self):
        self.assertEqual(trend_open(sample(spot_entry=100.2), CFG), "yes")
        self.assertEqual(trend_open(sample(spot_entry=99.8), CFG), "no")
        self.assertIsNone(trend_open(sample(spot_entry=100.0), CFG))

    def test_run_returns_trades_in_time_order(self):
        trades = run(trend_open, [sample(ticker="b", close_ts=2000), sample(ticker="a", close_ts=1000)], CFG)
        self.assertEqual([t.ticker for t in trades], ["a", "b"])


class Stats(unittest.TestCase):
    def test_summary(self):
        trades = [Trade("a", 1, "yes", 90.0, 1.0, True, 9.0), Trade("b", 2, "yes", 90.0, 1.0, False, -91.0),
                  Trade("c", 3, "yes", 90.0, 1.0, False, -91.0)]
        s = summarize(trades)
        self.assertEqual((s.n, s.losing_streak), (3, 2))
        self.assertAlmostEqual(s.mean, (9 - 91 - 91) / 3)
        self.assertAlmostEqual(s.roi, -173 / 273)
        self.assertEqual((s.avg_win, s.avg_loss), (9.0, -91.0))
        self.assertAlmostEqual(s.drawdown, 182.0)

    def test_empty_summary_is_safe(self):
        self.assertEqual(summarize([]).n, 0)

    def test_helpers(self):
        self.assertEqual(longest_run([True, True, False, True]), 2)
        self.assertEqual(max_drawdown([5, -3, -4, 10, -1]), 7)
        lo, hi = wilson(50, 100)
        self.assertTrue(lo < 0.5 < hi)
        self.assertAlmostEqual(norm_cdf(0), 0.5)


class Parsing(unittest.TestCase):
    def test_prices_in_cents_or_dollars(self):
        self.assertEqual(cents({"close": 56}, "close"), 56.0)
        self.assertAlmostEqual(cents({"close_dollars": "0.5600"}, "close"), 56.0)
        self.assertAlmostEqual(cents({"close": "0.6100"}, "close"), 61.0)  # historical endpoints: dollar string, plain key
        self.assertIsNone(cents({"close": None}, "close"))
        self.assertIsNone(cents(None, "close"))

    def test_candle_and_market(self):
        c = parse_candle({"end_period_ts": 1200, "yes_bid": {"close": 40}, "yes_ask": {"close_dollars": "0.45"},
                          "price": {"close": None}, "volume": 7})
        self.assertEqual((c.end_ts, c.bid, c.ask, c.last, c.volume), (1200, 40.0, 45.0, None, 7.0))
        old = parse_candle({"end_period_ts": 1200, "yes_bid": {"close": "0.6100"}, "yes_ask": {"close": "0.6200"},
                            "price": {"close": "0.6100", "previous": None}, "volume": "162679.92"})
        self.assertEqual((old.bid, old.ask, old.last), (61.0, 62.0, 61.0))
        m = parse_market({"ticker": "X", "close_time": "2026-10-20T08:30:00Z", "result": "yes", "floor_strike": 100000.5})
        self.assertEqual((m.ticker, m.yes_won, m.strike), ("X", True, 100000.5))
        self.assertIsNone(parse_market({"ticker": "X", "close_time": "2026-10-20T08:30:00Z", "result": ""}))


class Batching(unittest.TestCase):
    def test_batches_stay_under_the_requested_candle_cap(self):
        markets = [Market(f"T{i}", 900 * i, True, None) for i in range(1, 400)]
        batches = candle_batches(markets)
        self.assertEqual(sum(map(len, batches)), len(markets))
        for b in batches:
            span_min = (b[-1].close_ts - b[0].close_ts + 900 + 120) / 60
            self.assertLessEqual(len(b) * span_min, 10_000)
        self.assertGreater(len(batches[0]), 10)

    def test_a_gap_in_the_series_starts_a_new_batch(self):
        batches = candle_batches([Market("a", 900, True, None), Market("b", 900 * 500, True, None)])
        self.assertEqual([len(b) for b in batches], [1, 1])


class Spot(unittest.TestCase):
    def series(self, growth=0.001, n=200):
        minutes, price = {}, 100.0
        for k in range(n):
            minutes[k * 60] = (price, price * (1 + growth))
            price *= 1 + growth
        return SpotSeries(minutes)

    def test_price_at_a_boundary_is_the_close_of_the_minute_that_ends_there(self):
        s = SpotSeries({0: (10.0, 11.0), 60: (11.0, 12.0)})
        self.assertEqual(s.at(60), 11.0)
        self.assertEqual(s.at(120), 12.0)
        self.assertEqual(s.at(0), 10.0)  # no earlier candle: falls back to the open that starts there
        self.assertIsNone(s.at(600))

    def test_reference_is_the_mean_of_the_minute_before_the_boundary(self):
        s = SpotSeries({0: (10.0, 12.0), 60: (12.0, 20.0)})
        self.assertEqual(s.avg_minute_before(60), 11.0)
        self.assertEqual(s.avg_minute_before(120), 16.0)
        self.assertIsNone(s.avg_minute_before(300))

    def test_volatility_of_a_constant_growth_path(self):
        self.assertAlmostEqual(self.series().minute_vol(150 * 60, 60), math.log(1.001), places=6)

    def test_volatility_needs_enough_history(self):
        self.assertIsNone(self.series(n=20).minute_vol(20 * 60, 60))


class Build(unittest.TestCase):
    def candles(self, bid=58.0, ask=60.0, end=780):
        return [Candle(end, bid, ask, None, 5.0), Candle(end - 60 * MOM_MIN, 50.0, 52.0, None, 3.0)]

    def test_uses_the_quote_at_the_entry_minute_and_nothing_later(self):
        m = Market("T", 900, True, 100.0)
        late = Candle(840, 99.0, 100.0, None, 1.0)  # after the entry: must be ignored
        out, skipped = build_samples([m], lambda _: self.candles() + [late], None, lead_s=120)
        self.assertEqual((out[0].bid, out[0].ask, out[0].bid_prev, out[0].tau_min), (58.0, 60.0, 50.0, 2.0))
        self.assertFalse(skipped)

    def test_drops_are_counted(self):
        m = Market("T", 900, True, 100.0)
        cases = {"no candle at entry": [], "no two-sided quote": self.candles(bid=None),
                 "spread too wide": self.candles(bid=40.0, ask=60.0), "bad quote": self.candles(bid=70.0, ask=60.0)}
        for why, cs in cases.items():
            out, skipped = build_samples([m], lambda _: cs, None, lead_s=120)
            self.assertEqual((out, dict(skipped)), ([], {why: 1}))

    def test_spot_fields_come_from_the_right_moments(self):
        minutes = {k * 60: (100.0 + k, 100.0 + k + 1) for k in range(40)}  # close of minute k = 101 + k
        spot = SpotSeries(minutes)
        m = Market("T", 20 * 60 + 900, True, None)  # window starts at 1200 s, entry at 1200 + 780
        out, _ = build_samples([m], lambda _: self.candles(end=m.close_ts - 120), spot, lead_s=120)
        s = out[0]
        self.assertEqual(s.spot_open, spot.avg_minute_before(1200))
        self.assertEqual(s.spot_entry, spot.at(1200 + 780))
        self.assertEqual(s.spot_prev, spot.at(1200 + 780 - 180))
        self.assertEqual(s.spot_end, spot.avg_minute_before(m.close_ts))


def synthetic_worlds(n=3000, seed=7):
    """Random-walk BTC paths with a market maker who prices them (a) exactly and (b) 40% too close to 50/50.

    Synthetic and test-only: it checks that the pipeline sees an edge when one is planted and none when
    the market is fair. It says nothing about the real Kalshi market.
    """
    rng = random.Random(seed)
    sigma = 0.0007
    step = sigma / math.sqrt(60)
    fair, shrunk = [], []
    for i in range(n):
        path = [100_000.0]
        for _ in range(900):
            path.append(path[-1] * math.exp(rng.gauss(0.0, step)))
        yes_won = sum(path[840:901]) / 61 >= path[0]

        def p_true(sec):
            return norm_cdf(math.log(path[sec] / path[0]) / (sigma * math.sqrt((900 - sec) / 60 - 2 / 3)))

        for world, shrink in ((fair, 1.0), (shrunk, 0.6)):
            quote = lambda sec: 100 * (0.5 + shrink * (p_true(sec) - 0.5))
            mid, mid_prev = quote(780), quote(600)
            world.append(Sample(f"S{i}", 1000 + i * 900, yes_won, max(1.0, mid - 1), min(99.0, mid + 1), mid_prev - 1,
                                mid_prev + 1, 2.0, path[0], path[0], path[600], path[780], path[900], sigma))
    return fair, shrunk


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fair, cls.shrunk = synthetic_worlds()

    def test_fair_market_shows_no_edge_anywhere(self):
        _, verdicts = preregistered(self.fair, CFG)
        self.assertTrue(all(v.startswith("no ") for v in verdicts.values()), verdicts)
        for strat in list(PREREGISTERED.values()) + list(CONTROLS.values()):
            self.assertLess(summarize(run(strat, self.fair, CFG)).mean, 0.5)  # the spread and fee are a cost
        self.assertEqual(run(model_edge, self.fair, CFG), [])  # model equals the maker's price, so edge < spread + fee

    def test_planted_mispricing_is_found_by_the_model_and_not_by_noise(self):
        trades = run(model_edge, self.shrunk, CFG)
        s = summarize(trades)
        self.assertGreater(s.n, 1000)
        self.assertGreater(s.mean, 2.0)
        self.assertLess(s.p_pos, ALPHA)
        _, verdicts = preregistered(self.shrunk, CFG)
        self.assertTrue(verdicts["model_edge"].startswith("EDGE"), verdicts)

    def test_report_renders_with_and_without_btc_data(self):
        text = render(self.shrunk, Counter(), {120: self.shrunk}, CFG, len(self.shrunk), "t")
        for needle in ("Pre-registered strategies", "Price buckets", "Brier", "model_edge", "wins to repay 1 loss"):
            self.assertIn(needle, text)
        blind = [s.__class__(**{**s.__dict__, "spot_open": None, "spot_prev": None, "spot_entry": None, "spot_end": None,
                                "sigma": None}) for s in self.shrunk[:50]]
        self.assertIn("No samples with BTC data", render(blind, Counter(), {120: blind}, CFG, 50, "t"))

    def test_verdict_needs_enough_out_of_sample_trades(self):
        few = summarize([Trade("a", i, "yes", 50.0, 1.0, True, 49.0) for i in range(10)])
        self.assertTrue(verdict(few, few).startswith("no verdict"))


if __name__ == "__main__":
    unittest.main()
