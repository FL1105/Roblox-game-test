import unittest

from xvenue.arb import basis_ok, best_hedge, hedge_sides, kalshi_fee, poly_fee, sweep
from xvenue.venues import kalshi_event, poly_slug, hour_start, ET
from datetime import datetime


class Fees(unittest.TestCase):
    def test_polymarket_fee_matches_the_published_example(self):
        self.assertAlmostEqual(poly_fee(0.50, 100), 1.75)  # $1.75 per 100 shares at 50 cents
        self.assertAlmostEqual(poly_fee(0.10, 100), 0.07 * 100 * 0.1 * 0.9)
        self.assertEqual(poly_fee(0.5, 0), 0.0)

    def test_kalshi_fee_rounds_up_to_the_cent(self):
        self.assertEqual(kalshi_fee(0.50, 100), 1.75)
        self.assertEqual(kalshi_fee(0.50, 1), 0.02)  # 0.0175 rounds up to a cent
        self.assertEqual(kalshi_fee(0.99, 1), 0.01)


class Sweeping(unittest.TestCase):
    def test_walks_levels_and_stops_at_available_depth(self):
        asks = [(0.40, 10), (0.50, 10)]
        cost, fees, filled = sweep(asks, 15, poly_fee)
        self.assertEqual(filled, 15)
        self.assertAlmostEqual(cost, 10 * 0.40 + 5 * 0.50)
        self.assertAlmostEqual(fees, poly_fee(0.40, 10) + poly_fee(0.50, 5))
        self.assertEqual(sweep(asks, 50, poly_fee)[2], 20)


class Hedge(unittest.TestCase):
    def test_which_pair_covers_every_outcome(self):
        self.assertEqual(hedge_sides(100, 150), ("Up", "No"))
        self.assertEqual(hedge_sides(100, 100), ("Up", "No"))
        self.assertEqual(hedge_sides(100, 50), ("Down", "Yes"))

    def test_the_chosen_pair_pays_at_least_a_dollar_wherever_the_price_ends(self):
        # settle on one common price S; Poly Up pays if S >= kp, Down if S < kp; Kalshi Yes pays if S > k (treated as >=), No otherwise
        for kp, k in ((100, 130), (100, 100), (100, 60)):
            ps, ks = hedge_sides(kp, k)
            for s in range(0, 250, 5):
                poly = (s >= kp) if ps == "Up" else (s < kp)
                kal = (s >= k) if ks == "Yes" else (s < k)
                self.assertGreaterEqual(poly + kal, 1, (kp, k, s))

    def test_a_real_gap_is_found_and_sized_to_the_depth(self):
        poly = [(0.40, 100)]
        kalshi = [(0.50, 30), (0.70, 1000)]  # only 30 shares are cheap enough
        h = best_hedge(100, 150, poly, kalshi)
        self.assertEqual((h.poly_side, h.kalshi_side), ("Up", "No"))
        self.assertEqual(h.shares, 30)
        self.assertGreater(h.profit, 0)
        self.assertAlmostEqual(h.profit, 30 - (30 * 0.40 + 30 * 0.50) - poly_fee(0.40, 30) - kalshi_fee(0.50, 30))

    def test_the_size_search_always_tries_the_full_depth(self):
        from xvenue.arb import share_grid

        for cap in (1, 7, 50, 51, 100, 333, 1999):
            grid = share_grid(cap)
            self.assertEqual(grid[-1], cap)
            self.assertEqual(grid, sorted(set(grid)))
        self.assertEqual(share_grid(0), [])
        h = best_hedge(100, 150, [(0.30, 100)], [(0.30, 100)])  # profit keeps growing with size, so the best size is all 100
        self.assertEqual(h.shares, 100)

    def test_fees_can_turn_a_naive_arb_into_a_loss(self):
        h = best_hedge(100, 150, [(0.49, 50)], [(0.50, 50)])  # asks sum to 0.99: "arbitrage" before fees
        self.assertLess(h.top_sum, 1.0)
        self.assertLess(h.profit, 0)

    def test_nothing_to_buy_means_no_hedge(self):
        self.assertIsNone(best_hedge(100, 150, [], [(0.5, 10)]))
        self.assertIsNone(best_hedge(100, 150, [(0.5, 10)], []))

    def test_the_guarantee_needs_a_strike_gap_larger_than_the_source_difference(self):
        self.assertTrue(basis_ok(100, 40))
        self.assertFalse(basis_ok(20, 40))


class Ladder(unittest.TestCase):
    def row(self, k, ya, na, ya_size, yb_size):
        return {"floor_strike": k - 0.01, "yes_ask_dollars": str(ya), "no_ask_dollars": str(na), "yes_ask_size_fp": str(ya_size), "yes_bid_size_fp": str(yb_size)}

    def test_a_mispriced_ladder_is_found(self):
        from xvenue.ladder import ladder_arbs

        rows = [self.row(100, 0.30, 0.72, 500, 500), self.row(200, 0.31, 0.60, 500, 400)]  # Yes(100) 0.30 + No(200) 0.60 = 0.90
        arbs = ladder_arbs(rows)
        self.assertEqual((arbs[0].low, arbs[0].high), (100.0, 200.0))
        self.assertEqual(arbs[0].contracts, 400)  # limited by the No(200) side
        self.assertGreater(arbs[0].per_contract, 0.05)

    def test_an_efficient_ladder_has_nothing(self):
        from xvenue.ladder import ladder_arbs

        rows = [self.row(100, 0.61, 0.40, 500, 500), self.row(200, 0.41, 0.60, 500, 500)]
        self.assertEqual(ladder_arbs(rows), [])

    def test_fees_kill_a_one_cent_gap(self):
        from xvenue.ladder import ladder_arbs

        self.assertEqual(ladder_arbs([self.row(100, 0.50, 0.50, 500, 500), self.row(200, 0.49, 0.495, 500, 500)]), [])

    def test_poly_set(self):
        from xvenue.ladder import poly_set

        self.assertIsNone(poly_set([], [(0.5, 10)]))
        self.assertLess(poly_set([(0.52, 100)], [(0.50, 100)])[0], 0)  # 1.02 plus fees
        profit, shares, per = poly_set([(0.40, 100)], [(0.45, 100)])  # 0.85 plus fees
        self.assertGreater(profit, 0)
        self.assertEqual(shares, 100)


class Bankroll(unittest.TestCase):
    def test_a_budget_limits_the_hedge_size(self):
        poly, kalshi = [(0.30, 1000)], [(0.30, 1000)]
        big = best_hedge(100, 150, poly, kalshi)
        small = best_hedge(100, 150, poly, kalshi, budget=30.0)
        self.assertGreater(big.shares, small.shares)
        self.assertLessEqual(small.cost + small.fees, 30.0)

    def test_too_small_a_bankroll_cannot_buy_the_minimum_order(self):
        self.assertIsNone(best_hedge(100, 150, [(0.30, 1000)], [(0.30, 1000)], budget=2.0))  # fewer than Polymarket's 5 shares

    def test_ladder_and_set_respect_the_budget(self):
        from xvenue.ladder import ladder_arbs, poly_set

        rows = [{"floor_strike": 99.99, "yes_ask_dollars": "0.30", "no_ask_dollars": "0.72", "yes_ask_size_fp": "500", "yes_bid_size_fp": "500"},
                {"floor_strike": 199.99, "yes_ask_dollars": "0.31", "no_ask_dollars": "0.60", "yes_ask_size_fp": "500", "yes_bid_size_fp": "400"}]
        a = ladder_arbs(rows, budget=50.0)[0]
        self.assertLessEqual(a.cost, 50.0)
        self.assertLess(a.contracts, 400)
        self.assertEqual(ladder_arbs(rows, budget=0.5), [])
        p = poly_set([(0.40, 1000)], [(0.45, 1000)], budget=50.0)
        self.assertLessEqual(p[2] * p[1], 50.0 + 1e-6)
        self.assertIsNone(poly_set([(0.40, 1000)], [(0.45, 1000)], budget=2.0))

    def test_break_even_edge_barely_depends_on_bankroll_above_a_few_dollars(self):
        from xvenue.sizing import breakeven_edge

        mid = [breakeven_edge(0.5, b) for b in (100, 1000, 100000)]
        self.assertLess(max(mid) - min(mid), 0.002)  # under 0.2 cents
        self.assertGreater(mid[0], 0.03)  # about 3.5 cents at mid prices
        self.assertIsNone(breakeven_edge(0.5, 2.0))
        self.assertLess(breakeven_edge(0.5, 1000, poly_maker=True), mid[1] * 0.6)  # a maker leg roughly halves it
        self.assertLess(breakeven_edge(0.1, 1000), breakeven_edge(0.5, 1000))  # fees are smaller away from 50 cents


class Naming(unittest.TestCase):
    def test_polymarket_hourly_slug(self):
        self.assertEqual(poly_slug(datetime(2026, 10, 21, 19, tzinfo=ET)), "bitcoin-up-or-down-october-21-2026-7pm-et")
        self.assertEqual(poly_slug(datetime(2026, 10, 21, 0, tzinfo=ET)), "bitcoin-up-or-down-october-21-2026-12am-et")
        self.assertEqual(poly_slug(datetime(2026, 10, 21, 12, tzinfo=ET)), "bitcoin-up-or-down-october-21-2026-12pm-et")

    def test_kalshi_event_ticker(self):
        self.assertEqual(kalshi_event(datetime(2026, 10, 21, 20, tzinfo=ET)), "KXBTCD-26OCT2120")
        self.assertEqual(kalshi_event(datetime(2026, 11, 2, 9, tzinfo=ET)), "KXBTCD-26NOV0209")

    def test_hour_start_is_the_top_of_the_eastern_hour(self):
        h = hour_start(datetime(2026, 10, 21, 23, 4, 30, tzinfo=__import__("datetime").timezone.utc))
        self.assertEqual((h.hour, h.minute, h.second), (19, 0, 0))


if __name__ == "__main__":
    unittest.main()
