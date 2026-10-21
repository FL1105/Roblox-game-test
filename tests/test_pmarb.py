import unittest

from pmarb.detect import buy_all, cross_pair, implication, ladders, sell_all, shape
from pmarb.model import Fee, Leg, Quote, build
from pmarb.venues import best, kalshi_quote

NOFEE = Fee(0.0)
KALSHI = Fee(0.07, 1.0, cent_round=True)


def q(label, bid, ask, size=100, fee=NOFEE, **kw):
    base = dict(venue="kalshi", event="E", market=f"M-{label}", title="T", label=label, bid=bid, bid_size=size, ask=ask, ask_size=size, fee=fee)
    base.update(kw)
    return Quote(**base)


class Fees(unittest.TestCase):
    def test_kalshi_rounds_the_whole_order_up_to_the_cent(self):
        self.assertAlmostEqual(KALSHI.total(0.5, 10), 0.18)      # 0.07 * 10 * 0.25 = 0.175
        self.assertAlmostEqual(KALSHI.total(0.5, 100), 1.75)
        self.assertEqual(KALSHI.total(0.5, 0), 0.0)

    def test_polymarket_fee_is_the_published_table(self):
        crypto = Fee(0.07)
        self.assertAlmostEqual(crypto.total(0.5, 100), 1.75)     # docs: $1.75 at 50c for 100 shares
        self.assertAlmostEqual(crypto.total(0.1, 100), 0.63)
        self.assertAlmostEqual(crypto.total(0.9, 100), crypto.total(0.1, 100))   # symmetric around 50%
        self.assertEqual(Fee(0.0).total(0.5, 100), 0.0)          # geopolitics


class Sizing(unittest.TestCase):
    def test_size_is_the_thinnest_leg_and_minimum_order_is_respected(self):
        legs = [Leg("poly", "a", "a", "yes", 0.4, 50, NOFEE, 5), Leg("poly", "b", "b", "no", 0.5, 12, NOFEE, 5)]
        opp = build("t", "e", "t", legs, 1.0, True)
        self.assertEqual(opp.qty, 12)
        self.assertAlmostEqual(opp.net, 12 * (1 - 0.9))
        thin = [Leg("poly", "a", "a", "yes", 0.4, 3, NOFEE, 5), Leg("poly", "b", "b", "no", 0.5, 50, NOFEE, 5)]
        self.assertIsNone(build("t", "e", "t", thin, 1.0, True))


class Exclusive(unittest.TestCase):
    def test_selling_every_outcome_profits_when_the_bids_sum_above_one(self):
        quotes = [q("a", 0.5, 0.55), q("b", 0.4, 0.45), q("c", 0.3, 0.35)]    # bids sum 1.2
        opp = sell_all("E", "T", quotes)
        self.assertEqual(opp.payout_min, 2.0)
        self.assertAlmostEqual(opp.net / opp.qty, 0.2)
        self.assertTrue(opp.risk_free)

    def test_no_arbitrage_when_the_bids_sum_to_less_than_one(self):
        self.assertLess(sell_all("E", "T", [q("a", 0.4, 0.5), q("b", 0.3, 0.4), q("c", 0.2, 0.3)]).net, 0)

    def test_fees_can_erase_a_gross_edge(self):
        quotes = [q("a", 0.52, 0.55, fee=KALSHI), q("b", 0.5, 0.55, fee=KALSHI)]   # gross 2c per set
        self.assertLess(sell_all("E", "T", quotes).net, 0)

    def test_a_missing_bid_means_no_trade(self):
        self.assertIsNone(sell_all("E", "T", [q("a", None, 0.5), q("b", 0.5, 0.6)]))

    def test_buying_every_outcome_is_never_called_risk_free(self):
        opp = buy_all("E", "T", [q("a", 0.2, 0.3), q("b", 0.2, 0.3)])
        self.assertFalse(opp.risk_free)    # the listed outcomes may not be exhaustive
        self.assertGreater(opp.net, 0)


class Ladders(unittest.TestCase):
    def above(self, label, strike, bid, ask, rules="Above the number at 5pm", **kw):
        return q(label, bid, ask, strike_type="greater", strike=strike, close_ts=1000, rules=rules, **kw)

    def test_a_higher_threshold_cannot_be_likelier(self):
        lo, hi = self.above("100 or above", 100, 0.60, 0.62), self.above("110 or above", 110, 0.65, 0.70)
        opp = implication("E", "T", lo, hi)
        self.assertEqual([(l.side, l.label) for l in opp.legs], [("yes", "100 or above"), ("no", "110 or above")])
        self.assertAlmostEqual(opp.net / opp.qty, 1 - (0.62 + 0.35))
        found = ladders("E", "T", [lo, hi])
        self.assertEqual(len(found), 1)
        self.assertGreater(found[0].net, 0)

    def test_a_consistent_ladder_has_no_arbitrage(self):
        quotes = [self.above("100 or above", 100, 0.70, 0.72), self.above("110 or above", 110, 0.55, 0.57)]
        self.assertTrue(all(o.net <= 0 for o in ladders("E", "T", quotes)))

    def test_below_ladders_run_the_other_way(self):
        lo = q("under 100", 0.50, 0.55, strike_type="less", strike=100, close_ts=1000, rules="Below the number")
        hi = q("under 110", 0.30, 0.31, strike_type="less", strike=110, close_ts=1000, rules="Below the number")
        # P(< 100) cannot exceed P(< 110): bid(<100) = 0.50 > ask(<110) = 0.31
        opp = ladders("E", "T", [lo, hi])[0]
        self.assertEqual([(l.side, l.label) for l in opp.legs], [("yes", "under 110"), ("no", "under 100")])
        self.assertGreater(opp.net, 0)

    def test_different_subjects_in_one_event_are_never_compared(self):
        a = self.above("Player A: 15+", 15, 0.20, 0.25, rules="Player A scores 15 or more")
        b = self.above("Player B: 25+", 25, 0.60, 0.65, rules="Player B scores 25 or more")
        self.assertEqual(ladders("E", "T", [a, b]), [])    # the bug that produced $728k of fake profit

    def test_different_close_times_are_never_compared(self):
        a, b = self.above("x 1+", 1, 0.2, 0.25), self.above("x 2+", 2, 0.6, 0.65)
        b = Quote(**{**b.__dict__, "close_ts": 2000})
        self.assertEqual(ladders("E", "T", [a, b]), [])

    def test_shape_ignores_numbers_and_punctuation(self):
        self.assertEqual(shape("Al Horford: 15+"), shape("Al Horford: 20+"))
        self.assertNotEqual(shape("Al Horford: 15+"), shape("Luka Doncic: 15+"))


class Venue(unittest.TestCase):
    def market(self, **kw):
        base = {"ticker": "K-1", "yes_sub_title": "x", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4500", "yes_bid_size_fp": "10.00",
                "yes_ask_size_fp": "20.00", "close_time": "2026-10-21T20:00:00Z", "strike_type": "greater", "floor_strike": 5, "cap_strike": None}
        base.update(kw)
        return kalshi_quote({"event_ticker": "K", "title": "T"}, base, KALSHI)

    def test_a_threshold_has_one_bound(self):
        self.assertEqual(self.market().strike, 5)
        self.assertEqual(self.market(strike_type="less", floor_strike=None, cap_strike=7).strike, 7)

    def test_exact_count_bins_are_not_thresholds(self):
        # Kalshi tags 'exactly N' buckets as strike_type less with floor == cap; treating them as a ladder invented arbitrage.
        self.assertIsNone(self.market(strike_type="less", floor_strike=3, cap_strike=3).strike)
        self.assertIsNone(self.market(strike_type="greater", floor_strike=9, cap_strike=12).strike)

    def test_empty_sides_become_none(self):
        m = self.market(yes_bid_dollars="0.0000", yes_ask_dollars="1.0000")
        self.assertIsNone(m.bid)
        self.assertIsNone(m.ask)

    def test_best_levels_are_found_even_when_unsorted(self):
        levels = [{"price": "0.40", "size": "5"}, {"price": "0.45", "size": "7"}, {"price": "0.45", "size": "3"}, {"price": "0.30", "size": "0"}]
        self.assertEqual(best(levels, True), (0.45, 10.0))
        self.assertEqual(best(levels, False), (0.40, 5.0))
        self.assertEqual(best([], True), (None, 0.0))


class Cross(unittest.TestCase):
    def test_same_proposition_on_two_venues_both_directions(self):
        a = q("up", 0.38, 0.40, venue="kalshi")
        b = q("up", 0.45, 0.48, venue="poly", market="P")
        opps = cross_pair("cross", "T", a, b, same_resolution=True, note="")
        self.assertEqual(len(opps), 2)
        best_opp = max(opps, key=lambda o: o.net)
        self.assertEqual([(l.venue, l.side) for l in best_opp.legs], [("kalshi", "yes"), ("poly", "no")])
        self.assertAlmostEqual(best_opp.net / best_opp.qty, 1 - (0.40 + 0.55))
        self.assertTrue(best_opp.risk_free)
        self.assertFalse(cross_pair("cross", "T", a, b, same_resolution=False, note="basis")[0].risk_free)


if __name__ == "__main__":
    unittest.main()


class WatchSummary(unittest.TestCase):
    def row(self, t, label, net, gross=0.0, maker=0.0, kind="game"):
        return {"t": t, "label": label, "kind": kind, "k_bid": 0.4, "k_ask": 0.42, "p_bid": 0.41, "p_ask": 0.43,
                "gross": gross, "maker_edge": maker, "net": net, "qty": 10, "fees": 0.1}

    def test_episodes_count_consecutive_polls_and_take_the_best_moment(self):
        from pmarb.summary import episodes
        rows = [self.row(1, "a", -1), self.row(2, "a", 2.0), self.row(3, "a", 5.0), self.row(4, "a", -1), self.row(5, "a", 1.0),
                self.row(1, "b", -1), self.row(2, "b", -1), self.row(3, "b", -1)]
        eps = sorted(episodes(rows, "net", lambda r: r["net"] > 0))
        self.assertEqual(eps, [("a", 1, 1.0), ("a", 2, 5.0)])

    def test_summary_separates_kinds_and_counts_fee_survivors(self):
        from pmarb.summary import summarise
        rows = [self.row(1, "a", -0.5, gross=0.01, maker=0.02), self.row(2, "a", -0.4, gross=0.0, maker=0.0),
                self.row(1, "b", 3.0, gross=0.08, kind="btc15"), self.row(2, "b", None, gross=None, maker=None, kind="btc15")]
        s = summarise(rows)
        self.assertEqual(s["cycles"], 2)
        self.assertEqual(s["by_kind"]["game"]["gross_positive"], 1)
        self.assertEqual(s["by_kind"]["game"]["net_positive"], 0)
        self.assertEqual(s["by_kind"]["game"]["maker_ge_2c"], 1)
        self.assertEqual(s["by_kind"]["btc15"]["net_positive"], 1)
        self.assertAlmostEqual(s["by_kind"]["btc15"]["net_total_if_each_episode_taken_once"], 3.0)
