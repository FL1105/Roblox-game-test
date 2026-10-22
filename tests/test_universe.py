import math
import unittest

from xvenue.universe import Finding, Leg, kalshi_findings, nests, poly_findings


def km(ticker, ya, na, ysz=100, yb_sz=100, strike_type="custom", floor=None, cap=None, rules="", status="active"):
    return {"ticker": ticker, "status": status, "yes_ask_dollars": str(ya), "no_ask_dollars": str(na), "yes_ask_size_fp": str(ysz), "yes_bid_size_fp": str(yb_sz),
            "strike_type": strike_type, "floor_strike": floor, "cap_strike": cap, "rules_primary": rules}


def mk(ticker, na, spread=0.02, **kw):
    """A market with a realistic book: Yes ask = 1 - No ask + spread, so the two sides alone are never an arbitrage."""
    return km(ticker, round(1 - na + spread, 4), na, **kw)


def ev(markets, exclusive=False):
    return {"event_ticker": "EV", "title": "t", "mutually_exclusive": exclusive, "markets": markets}


class KalshiAllNo(unittest.TestCase):
    def test_no_in_every_market_of_an_exclusive_event(self):
        e = ev([mk("A", 0.30), mk("B", 0.40), mk("C", 0.50)], exclusive=True)  # No asks sum to 1.20; the bundle pays at least n - 1 = 2
        f = [x for x in kalshi_findings(e) if x.kind == "all-no"][0]
        self.assertEqual(f.payout, 2.0)
        self.assertAlmostEqual(f.unit_cost, 1.20)
        self.assertGreater(f.best()[1], 0)

    def test_an_efficient_event_has_nothing(self):
        e = ev([mk("A", 0.55), mk("B", 0.55)], exclusive=True)  # No asks sum 1.10 > 1
        self.assertTrue(all(x.best()[1] == 0 for x in kalshi_findings(e)))

    def test_not_exclusive_means_no_bundle(self):
        e = ev([mk("A", 0.30), mk("B", 0.40)], exclusive=False)
        self.assertEqual(kalshi_findings(e), [])

    def test_an_untradable_market_breaks_the_bundle(self):
        e = ev([mk("A", 0.30), mk("B", 0.40), mk("C", 0.50, status="closed")], exclusive=True)
        self.assertEqual([x for x in kalshi_findings(e) if x.kind == "all-no"], [])

    def test_empty_books_are_ignored(self):
        e = ev([mk("A", 0.30, yb_sz=0), mk("B", 0.40)], exclusive=True)
        self.assertEqual([x for x in kalshi_findings(e) if x.kind == "all-no"], [])


class KalshiLadder(unittest.TestCase):
    ABOVE = "If the average price is above {} at 5 PM, then the market resolves to Yes."

    def ladder_event(self, a_floor, a_ya, a_na, b_floor, b_ya, b_na):
        return ev([km("LO", a_ya, a_na, strike_type="greater", floor=a_floor, rules=self.ABOVE.format(a_floor)),
                   km("HI", b_ya, b_na, strike_type="greater", floor=b_floor, rules=self.ABOVE.format(b_floor))])

    def test_a_threshold_ladder_pair_is_found(self):
        fs = [f for f in kalshi_findings(self.ladder_event(100, 0.30, 0.72, 200, 0.40, 0.55)) if f.kind == "ladder"]  # Yes(100) 0.30 + No(200) 0.55
        self.assertEqual([l.label for l in fs[0].legs], ["Yes LO", "No HI"])

    def test_exact_value_brackets_are_not_a_ladder(self):
        # regression: 'exactly 3 launches' and 'exactly 8 launches' are exclusive brackets even though Kalshi tags them 'greater'
        e = ev([km("X3", 0.40, 0.60, strike_type="greater", floor=3, rules="If exactly 3 launches reach space, then Yes."),
                km("X8", 0.02, 0.60, strike_type="greater", floor=8, rules="If exactly 8 launches reach space, then Yes.")])
        self.assertEqual([f for f in kalshi_findings(e) if f.kind == "ladder"], [])

    def test_different_quantities_are_not_a_ladder(self):
        # regression: a spread event lists each team's line; 'NYG wins by over 3' and 'HOU wins by over 3' do not nest
        e = ev([km("NYG", 0.10, 0.80, strike_type="greater", floor=3, rules="If New York G wins by over 3 points, then Yes."),
                km("HOU", 0.10, 0.80, strike_type="greater", floor=4, rules="If Houston wins by over 4 points, then Yes.")])
        self.assertEqual([f for f in kalshi_findings(e) if f.kind == "ladder"], [])

    def test_wording_gate(self):
        self.assertTrue(nests("greater", "is above 72999.99"))
        self.assertTrue(nests("less", "falls below 70,000"))
        self.assertFalse(nests("greater", "exactly 8"))
        self.assertFalse(nests("greater", "between 1 and 2, above 0"))
        self.assertFalse(nests("less", "is above 5"))

    def test_less_than_ladders_are_mirrored(self):
        rules = "If the price is below {} then Yes."
        e = ev([km("L1", 0.20, 0.85, strike_type="less", cap=100, rules=rules.format(100)), km("L2", 0.50, 0.30, strike_type="less", cap=200, rules=rules.format(200))])
        # Yes(below 200) + No(below 100) covers everything: 0.50 + 0.85 > 1, so no arb; make it cheap
        self.assertEqual([f for f in kalshi_findings(e) if f.kind == "ladder"], [])
        e = ev([km("L1", 0.20, 0.40, strike_type="less", cap=100, rules=rules.format(100)), km("L2", 0.50, 0.30, strike_type="less", cap=200, rules=rules.format(200))])
        f = [f for f in kalshi_findings(e) if f.kind == "ladder"][0]
        self.assertEqual([l.label for l in f.legs], ["Yes L2", "No L1"])


class Fees(unittest.TestCase):
    def test_fee_waived_series_keep_thin_gaps(self):
        e = ev([mk("A", 0.48), mk("B", 0.48)], exclusive=True)  # No asks 0.96 for payout 1
        paying = [f for f in kalshi_findings(e, 1.0) if f.kind == "all-no"][0]
        waived = [f for f in kalshi_findings(e, 0.0) if f.kind == "all-no"][0]
        self.assertGreater(waived.best()[1], paying.best()[1])
        self.assertGreater(waived.best()[1], 0)

    def test_kalshi_fee_rounds_up_per_order(self):
        self.assertEqual(Leg("kalshi", "x", 0.5, 0.07).fee(1), 0.02)
        self.assertAlmostEqual(Leg("polymarket", "x", 0.5, 0.07).fee(1), 0.0175)
        self.assertEqual(Leg("polymarket", "x", 0.5, 0.0).fee(100), 0.0)


class Sizing(unittest.TestCase):
    def test_bankroll_limits_size_and_never_overspends(self):
        f = Finding("kalshi", "all-no", "E", "t", (Leg("kalshi", "a", 0.30, 0.07), Leg("kalshi", "b", 0.60, 0.07), Leg("kalshi", "c", 0.95, 0.07)), 2.0, 400)
        units, _ = f.best(100)
        self.assertLessEqual(f.cost(units), 100)
        self.assertGreater(f.best()[0], f.best(100)[0])
        self.assertGreater(f.best()[1], f.best(100)[1])
        self.assertEqual(f.best(1.0), (0.0, 0.0))

    def test_no_profit_means_no_size(self):
        f = Finding("kalshi", "all-no", "E", "t", (Leg("kalshi", "a", 0.60, 0.07), Leg("kalshi", "b", 0.45, 0.07)), 1.0, 100)
        self.assertEqual(f.best(), (0.0, 0.0))


class Polymarket(unittest.TestCase):
    def market(self, slug, bid, ask, fees=False):
        return {"slug": slug, "active": True, "closed": False, "enableOrderBook": True, "acceptingOrders": True, "bestBid": bid, "bestAsk": ask, "feesEnabled": fees,
                "clobTokenIds": '["y","n"]'}

    def test_neg_risk_all_yes_is_flagged_conditional(self):
        e = {"slug": "e", "title": "t", "negRisk": True, "markets": [self.market("a", 0.30, 0.31), self.market("b", 0.40, 0.41), self.market("c", 0.20, 0.25)]}  # asks 0.97
        f = [x for x in poly_findings(e) if x.kind == "all-yes"][0]
        self.assertTrue(f.risk)

    def test_neg_risk_all_no_needs_bids_above_one_and_has_no_caveat(self):
        e = {"slug": "e", "title": "t", "negRisk": True, "markets": [self.market("a", 0.50, 0.55), self.market("b", 0.52, 0.56)]}  # bids sum 1.02
        f = [x for x in poly_findings(e) if x.kind == "all-no"][0]
        self.assertFalse(f.risk)
        self.assertAlmostEqual(f.unit_cost, 0.98)

    def test_independent_markets_are_never_bundled(self):
        e = {"slug": "e", "title": "t", "negRisk": False, "markets": [self.market("a", 0.50, 0.55), self.market("b", 0.52, 0.30)]}
        self.assertEqual([x for x in poly_findings(e) if x.kind != "crossed"], [])

    def test_a_crossed_book_is_found(self):
        e = {"slug": "e", "title": "t", "negRisk": False, "markets": [self.market("a", 0.60, 0.55)]}
        self.assertEqual([x.kind for x in poly_findings(e)], ["crossed"])
        self.assertTrue(math.isclose(poly_findings(e)[0].unit_cost, 0.55 + 0.40))


if __name__ == "__main__":
    unittest.main()
