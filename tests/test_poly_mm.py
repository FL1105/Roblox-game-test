import unittest
from dataclasses import replace

from poly_mm.markets import Market, window_starts
from poly_mm.model import Quoter, floor_tick, pressure, simulate, summarize
from poly_mm.tape import Trade

M = Market("btc-updown-5m-1000", 1000, "0xc", "up", "down", winner=0)  # Up won; window 1000..1300
STATIC = Quoter(delta=0.02, size=50.0, refresh_s=1e9, latency_s=0.0, stop_before_s=0.0, fair="static", queue=0.5)


def trade(t, outcome, side, price, size):
    return Trade(t, outcome, side, price, size)


class Mapping(unittest.TestCase):
    def test_a_buy_of_one_outcome_is_a_sale_of_the_other(self):
        (t, side, x, sz), = pressure([trade(5, 0, "BUY", 0.55, 10)])
        self.assertEqual((t, side, sz), (5, 1, 10))
        self.assertAlmostEqual(x, 0.45)
        self.assertEqual(pressure([trade(5, 1, "SELL", 0.30, 4)]), [(5, 1, 0.30, 4)])

    def test_tick_rounding_goes_down(self):
        self.assertAlmostEqual(floor_tick(0.4799), 0.47)
        self.assertAlmostEqual(floor_tick(0.48), 0.48)

    def test_windows_are_resolved_and_aligned(self):
        starts = window_starts(100_000, 1, skip_recent=3)
        self.assertTrue(all(s % 300 == 0 for s in starts))
        self.assertLess(starts[-1] + 300, 100_000 - 600)


class Fills(unittest.TestCase):
    def test_a_sale_below_our_bid_fills_us_for_the_shares_sold_only(self):
        r = simulate(M, [trade(1100, 0, "SELL", 0.40, 8)], STATIC)  # we bid 0.48 on Up; 8 shares were sold at 0.40
        self.assertEqual((r.up, r.down), (8.0, 0.0))
        self.assertAlmostEqual(r.cost, 8 * 0.48)

    def test_a_sale_above_our_bid_does_not_fill(self):
        r = simulate(M, [trade(1100, 0, "SELL", 0.50, 80)], STATIC)
        self.assertEqual(r.fills, 0)

    def test_a_sale_at_our_price_fills_a_share_of_it(self):
        r = simulate(M, [trade(1100, 0, "SELL", 0.48, 10)], STATIC)  # queue = 0.5
        self.assertEqual(r.up, 5.0)

    def test_the_bid_never_fills_beyond_its_size(self):
        r = simulate(M, [trade(1100 + i, 0, "SELL", 0.40, 30) for i in range(5)], STATIC)
        self.assertEqual(r.up, 50.0)

    def test_buying_the_other_outcome_fills_our_bid(self):
        r = simulate(M, [trade(1100, 1, "BUY", 0.55, 6)], STATIC)  # a Down buy at 0.55 sells Up at 0.45, below our 0.48 bid
        self.assertEqual((r.up, r.down), (6.0, 0.0))
        r = simulate(M, [trade(1100, 1, "BUY", 0.52, 6)], STATIC)  # at 0.52 it sells Up at exactly 0.48: only our queue share (0.5)
        self.assertEqual(r.up, 3.0)

    def test_nothing_fills_after_the_cutoff_or_before_the_window(self):
        q = replace(STATIC, stop_before_s=60)
        r = simulate(M, [trade(1250, 0, "SELL", 0.40, 8), trade(900, 0, "SELL", 0.40, 8)], q)
        self.assertEqual(r.fills, 0)


class Settlement(unittest.TestCase):
    def test_a_completed_pair_earns_the_gap_whoever_wins(self):
        tape = [trade(1100, 0, "SELL", 0.40, 20), trade(1110, 1, "SELL", 0.40, 20)]
        for winner in (0, 1):
            r = simulate(replace(M, winner=winner), tape, STATIC)
            self.assertAlmostEqual(r.pnl, 20 - 40 * 0.48)  # $1 per pair for 0.96 paid

    def test_one_sided_inventory_wins_or_loses_everything(self):
        tape = [trade(1100, 0, "SELL", 0.40, 10)]
        self.assertAlmostEqual(simulate(replace(M, winner=0), tape, STATIC).pnl, 10 - 4.8)
        self.assertAlmostEqual(simulate(replace(M, winner=1), tape, STATIC).pnl, -4.8)


class Quoting(unittest.TestCase):
    def test_following_the_price_moves_the_bids(self):
        q = Quoter(delta=0.02, size=50.0, refresh_s=10, latency_s=0.0, stop_before_s=0.0, fair="last", queue=1.0)
        # Up trades at 0.80, so bids sit at 0.78 (Up) and 0.18 (Down): a Down sale at 0.17 fills us, a Down sale at 0.30 does not
        tape = [trade(1000, 0, "BUY", 0.80, 1), trade(1050, 1, "SELL", 0.30, 5), trade(1051, 1, "SELL", 0.17, 5)]
        r = simulate(M, tape, q)
        self.assertEqual((r.down, r.up), (5.0, 0.0))

    def test_a_stale_quote_gets_picked_off(self):
        q = Quoter(delta=0.02, size=50.0, refresh_s=10, latency_s=0.0, stop_before_s=0.0, fair="last", queue=1.0)
        # the market jumps to Up 0.80 at 1001 but our 0.48 / 0.48 bids from t=1000 are still up: the Down seller hits the Down bid
        r = simulate(M, [trade(1001, 0, "BUY", 0.80, 7)], q)
        self.assertEqual((r.down, r.up), (7.0, 0.0))

    def test_latency_delays_new_quotes(self):
        q = Quoter(delta=0.02, size=50.0, refresh_s=1e9, latency_s=30.0, stop_before_s=0.0, fair="static", queue=1.0)
        self.assertEqual(simulate(M, [trade(1010, 0, "SELL", 0.40, 5)], q).fills, 0)  # not live yet
        self.assertEqual(simulate(M, [trade(1040, 0, "SELL", 0.40, 5)], q).fills, 1)

    def test_skew_leans_against_inventory(self):
        q = replace(STATIC, refresh_s=10, fair="static", skew=0.001)
        # buy 10 Up at 0.48 first; at the next refresh the Up bid drops by 10 * 0.001 = one cent, so a sale at 0.475 no longer fills
        tape = [trade(1001, 0, "SELL", 0.40, 10), trade(1030, 0, "SELL", 0.475, 5)]
        r = simulate(M, tape, q)
        self.assertEqual(r.up, 10.0)


class Summary(unittest.TestCase):
    def test_summary_numbers(self):
        a = simulate(M, [trade(1100, 0, "SELL", 0.40, 10)], STATIC)  # wins 5.2
        b = simulate(replace(M, winner=1), [trade(1100, 0, "SELL", 0.40, 10)], STATIC)  # loses 4.8
        s = summarize([a, b])
        self.assertEqual(s.markets, 2)
        self.assertAlmostEqual(s.pnl, 0.4)
        self.assertAlmostEqual(s.win_rate, 0.5)
        self.assertAlmostEqual(s.worst, -4.8)


if __name__ == "__main__":
    unittest.main()
