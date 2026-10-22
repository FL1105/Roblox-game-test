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


class Sizing(unittest.TestCase):
    def test_kalshi_rounding_punishes_tiny_orders_at_extreme_prices(self):
        from pmarb.sizing import fee_per_contract, overhead
        self.assertAlmostEqual(fee_per_contract(0.97, 1), 0.01)             # the 1c minimum
        self.assertAlmostEqual(fee_per_contract(0.97, 100), 0.0021)          # ceil(20.37c) = 21c over 100
        self.assertGreater(overhead(0.97, 1), 3.5)
        self.assertLess(overhead(0.97, 1000), 0.01)
        self.assertAlmostEqual(fee_per_contract(0.5, 100), 0.0175)

    def test_walking_a_book_costs_the_marginal_levels(self):
        from pmarb.sizing import cost_to_buy, hedge_net
        self.assertAlmostEqual(cost_to_buy([(0.4, 10), (0.5, 10)], 15), 4.0 + 2.5)
        self.assertIsNone(cost_to_buy([(0.4, 10), (0.5, 10)], 25))
        self.assertAlmostEqual(hedge_net([(0.4, 100)], [(0.5, 100)], NOFEE, NOFEE, 10), 1.0)
        self.assertIsNone(hedge_net([(0.4, 100)], [(0.5, 100)], NOFEE, NOFEE, 200))
        self.assertAlmostEqual(hedge_net([(0.4, 5), (0.45, 5)], [(0.5, 10)], NOFEE, NOFEE, 10), 10 - 4.25 - 5.0)

    def test_hedge_net_charges_fees_on_the_average_fill(self):
        from pmarb.sizing import hedge_net
        fee = Fee(0.07)
        net = hedge_net([(0.4, 100)], [(0.5, 100)], NOFEE, fee, 100)
        self.assertAlmostEqual(net, 100 - 40 - 50 - 0.07 * 100 * 0.25)

    def test_capital_is_split_between_venues(self):
        from pmarb.sizing import capital_cap
        self.assertEqual(capital_cap({"k": 0.5, "p": 0.4}, 100), 100)       # min(50 / 0.5, 50 / 0.4)
        self.assertEqual(capital_cap({"k": 0.9, "p": 0.1}, 100), 55)        # the Kalshi leg is the binding one

    def test_repricing_an_opportunity_at_smaller_sizes(self):
        from pmarb.sizing import net_at
        legs = [Leg("kalshi", "k", "k", "yes", 0.4, 100, KALSHI), Leg("poly", "p", "p", "no", 0.5, 100, NOFEE, 5)]
        opp = build("t", "e", "t", legs, 1.0, True)
        self.assertAlmostEqual(net_at(opp, 100), opp.net)
        self.assertAlmostEqual(net_at(opp, 10), 10 * 0.1 - KALSHI.total(0.4, 10))
        self.assertIsNone(net_at(opp, 4))          # under the Polymarket minimum order
        self.assertIsNone(net_at(opp, 101))        # more than the book shows


class Bankroll(unittest.TestCase):
    def logged(self, t, label, k_bid, k_ask, p_bid, p_ask, qty, kalshi_mult=1.0):
        # a profitable Kalshi-YES + Polymarket-NO observation, with the fees and net the watcher would have logged
        gross, pk = p_bid - k_ask, k_ask
        fees = KALSHI.total(pk, qty) * kalshi_mult
        return {"t": t, "label": label, "kind": "game", "k_bid": k_bid, "k_ask": k_ask, "p_bid": p_bid, "p_ask": p_ask,
                "gross": gross, "maker_edge": 0.0, "net": qty * gross - fees, "qty": qty, "fees": fees}

    def test_direction_and_fee_multiplier_are_recovered_from_the_log(self):
        from pmarb.bankroll import identify
        r = self.logged(1, "g [A]", 0.44, 0.45, 0.50, 0.51, 200)
        direction, mult = identify(r)
        self.assertEqual(direction, 0)
        self.assertAlmostEqual(mult, 1.0, delta=0.1)

    def test_small_bankrolls_take_fewer_sets_and_big_ones_hit_the_depth_cap(self):
        from pmarb.bankroll import simulate
        rows = [self.logged(1, "g [A]", 0.44, 0.45, 0.50, 0.51, 500)]
        small, mid, big = (simulate(rows, b) for b in (100, 300, 100_000))
        self.assertEqual(len(small), 1)
        self.assertLess(small[0][2], mid[0][2])
        self.assertEqual(big[0][2], 500)             # capped by what the book showed
        self.assertGreater(big[0][3], mid[0][3])

    def test_capital_stays_locked_until_the_end(self):
        from pmarb.bankroll import simulate
        rows = [self.logged(1, "g [A]", 0.44, 0.45, 0.50, 0.51, 500), self.logged(21, "h [B]", 0.44, 0.45, 0.50, 0.51, 500)]
        one = simulate(rows, 400)          # enough for roughly one game's hedge at this size
        self.assertEqual(len(one), 1)      # the second opportunity finds the capital already spent
        self.assertEqual(len(simulate(rows, 100_000)), 2)

    def test_a_continuing_episode_is_one_trade(self):
        from pmarb.bankroll import simulate
        rows = [self.logged(t, "g [A]", 0.44, 0.45, 0.50, 0.51, 100) for t in (1, 21, 41)]
        self.assertEqual(len(simulate(rows, 100_000)), 1)


class Limitless(unittest.TestCase):
    def test_buy_fee_curve_interpolates_the_published_table(self):
        from pmarb.btc3 import limitless_buy_rate
        self.assertAlmostEqual(limitless_buy_rate(0.30), 0.03)       # flat 3.00% up to 50c
        self.assertAlmostEqual(limitless_buy_rate(0.60), 0.0213)     # table: $0.60 -> 2.13%
        self.assertAlmostEqual(limitless_buy_rate(0.90), 0.0068)
        self.assertAlmostEqual(limitless_buy_rate(0.625), (2.13 + 1.80) / 2 / 100)   # midway between 60c and 65c
        self.assertLess(limitless_buy_rate(0.95), limitless_buy_rate(0.60))

    def test_the_fee_is_taken_in_tokens_so_cost_per_token_rises(self):
        from pmarb.btc3 import buy_cost
        self.assertAlmostEqual(buy_cost("limitless", 0.40), 0.40 / 0.97)
        self.assertGreater(buy_cost("limitless", 0.40), 0.40)
        self.assertAlmostEqual(buy_cost("kalshi", 0.50, Fee(0.07, 1.0, cent_round=False)), 0.50 + 0.07 * 0.25)
        self.assertAlmostEqual(buy_cost("poly", 0.50, NOFEE), 0.50)


class SizedHedge(unittest.TestCase):
    def test_sets_are_the_thinner_side_and_fees_hit_both_legs(self):
        from pmarb.btc3 import hedge_total
        k = {"bid": 0.41, "bid_size": 9000, "ask": 0.42, "ask_size": 4400, "min": 1}
        p = {"bid": 0.53, "bid_size": 125.4, "ask": 0.54, "ask_size": 2000, "min": 5}
        kf, pf = Fee(0.07, 1.0, cent_round=True), Fee(0.07)
        h = hedge_total("kalshi", k, "poly", p, kf, pf)          # YES on Kalshi at .42, NO on Polymarket at 1 - .53 = .47
        self.assertEqual((h["sets"], h["yes_on"], h["no_on"]), (125, "kalshi", "poly"))
        cost = 125 * 0.42 + 125 * 0.47 + kf.total(0.42, 125) + pf.total(0.47, 125)
        self.assertAlmostEqual(h["net"], 125 - cost)
        self.assertAlmostEqual(h["gross"], 125 * (1 - 0.42 - 0.47))

    def test_minimum_orders_and_missing_sides_block_the_trade(self):
        from pmarb.btc3 import hedge_total
        thin = {"bid": 0.50, "bid_size": 8.0, "ask": 0.55, "ask_size": 48.0, "min": 50}        # Limitless-style: 50-share minimum
        deep = {"bid": 0.40, "bid_size": 900, "ask": 0.45, "ask_size": 900, "min": 1}
        self.assertIsNone(hedge_total("kalshi", deep, "limitless", thin, KALSHI, None))        # only 8 shares to hit: under the minimum
        self.assertIsNone(hedge_total("kalshi", {**deep, "ask": None}, "poly", deep, KALSHI, NOFEE))

    def test_limitless_buys_cost_more_than_the_quoted_price(self):
        from pmarb.btc3 import hedge_total
        a = {"bid": 0.40, "bid_size": 900, "ask": 0.40, "ask_size": 900, "min": 1}
        b = {"bid": 0.60, "bid_size": 900, "ask": 0.62, "ask_size": 900, "min": 1}
        free = hedge_total("kalshi", a, "kalshi", b, NOFEE, NOFEE)["net"]
        lim = hedge_total("limitless", a, "kalshi", b, None, NOFEE)["net"]
        self.assertLess(lim, free)


class Band(unittest.TestCase):
    def test_both_orientations_are_priced_with_fees(self):
        from pmarb.band import options
        a, b = options(0.40, 0.42, 0.55, 0.005)       # Kalshi 40/42, Polymarket 54.5/55.5
        self.assertEqual((a.yes_on, a.no_on, b.yes_on, b.no_on), ("kalshi", "poly", "poly", "kalshi"))
        # YES on Kalshi at .42 + NO on Polymarket at 1 - .545 = .455
        self.assertAlmostEqual(a.net, 1 - 0.42 - 0.455 - 0.07 * 0.42 * 0.58 - 0.07 * 0.455 * 0.545)
        self.assertLess(b.net, a.net)                 # the other direction pays Polymarket's ask for Kalshi's bid: a loser

    def test_payoff_is_zero_in_the_band_and_two_the_other_way(self):
        from pmarb.band import Option, payoff
        o = Option("kalshi", "poly", 0.1)             # YES Kalshi, NO Polymarket
        self.assertEqual(payoff(o, kalshi_up=True, poly_up=True), 1)
        self.assertEqual(payoff(o, kalshi_up=False, poly_up=False), 1)
        self.assertEqual(payoff(o, kalshi_up=True, poly_up=False), 2)      # the venues disagree in this hedge's favour
        self.assertEqual(payoff(o, kalshi_up=False, poly_up=True), 0)      # ... and against it

    def test_stale_polymarket_prices_are_not_used(self):
        from pmarb.band import poly_price_at
        h = [{"t": 100, "p": 0.4}, {"t": 180, "p": 0.5}]
        self.assertEqual(poly_price_at(h, 200), 0.5)
        self.assertIsNone(poly_price_at(h, 400))      # last point is older than 150 s
        self.assertIsNone(poly_price_at([], 200))

    def test_realised_equals_modeled_net_plus_payoff_minus_one(self):
        from pmarb.band import Option, score
        rows = [{"opt": Option("kalshi", "poly", 0.05), "payoff": 1}, {"opt": Option("kalshi", "poly", 0.05), "payoff": 0},
                {"opt": Option("kalshi", "poly", 0.01), "payoff": 1}]
        s = score(rows, 0.02)
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["mean"], (0.05 + (0.05 - 1)) / 2)
        self.assertEqual(s["payoffs"], {0: 1, 1: 1, 2: 0})
        self.assertEqual(score(rows, 0.5), {"n": 0})


class LeadLag(unittest.TestCase):
    def row(self, t, k, p, net=None, close=1000):
        q = lambda x: {"bid": x - 0.005, "ask": x + 0.005, "bid_size": 100, "ask_size": 100}
        return {"t": t, "close": close, "quotes": {"kalshi": q(k), "poly": q(p)}, "best": None if net is None else {"net": net}}

    def test_a_follower_is_closest_to_the_leader_a_few_polls_earlier(self):
        from pmarb.leadlag import lagged_gap
        lead = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.7, 0.6, 0.5, 0.4]
        rows = [self.row(6 * i, lead[max(i - 2, 0)], lead[i]) for i in range(len(lead))]     # Kalshi repeats Polymarket two polls late
        gaps = [lagged_gap(rows, "kalshi", "poly", k)[0] for k in range(0, 5)]
        self.assertEqual(gaps.index(min(gaps)), 2)
        self.assertLess(min(gaps), 1e-9)
        reverse = [lagged_gap(rows, "poly", "kalshi", k)[0] for k in range(0, 5)]
        self.assertEqual(reverse.index(min(reverse)), 0)

    def test_lags_never_cross_a_window_boundary(self):
        from pmarb.leadlag import lagged_gap
        rows = [self.row(0, 0.3, 0.3, close=1000), self.row(6, 0.9, 0.9, close=1900)]
        self.assertEqual(lagged_gap(rows, "kalshi", "poly", 1)[1], 0)

    def test_runs_and_first_sight_entries(self):
        from pmarb.leadlag import first_sight_entries, profitable_runs
        nets = [None, 1.0, 2.0, -1.0, 3.0, 4.0, 5.0, None]
        rows = [self.row(6 * i, 0.5, 0.5, n) for i, n in enumerate(nets)]
        self.assertEqual(profitable_runs(rows), [2, 3])
        self.assertEqual([r["t"] for r in first_sight_entries(rows)], [6, 24])
        far = [self.row(0, 0.5, 0.5, 1.0), self.row(60, 0.5, 0.5, 1.0)]       # a minute apart: two runs, not one
        self.assertEqual(profitable_runs(far), [1, 1])
