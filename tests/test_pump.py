import os
import tempfile
import unittest

from sniper.pump.curve import CurveState, quote_buy, quote_sell
from sniper.pump.events import NewToken, ScanNote, Trade, decode, encode, parse_pumpportal, read_jsonl, write_jsonl
from sniper.pump.store import DAY_MS, LaunchStore, Outcome, OutcomeTracker

from .sim import Sim

START = CurveState(30.0, 1_073_000_000.0)


class CurveMaths(unittest.TestCase):
    def test_launch_market_cap_is_about_28_sol(self):
        self.assertAlmostEqual(START.mcap_sol, 27.96, places=1)

    def test_buying_raises_price_and_round_trip_loses_fees_and_impact(self):
        tokens = quote_buy(START, 1.0, 100)
        after = CurveState(START.v_sol + 0.99, START.v_tokens - tokens)
        self.assertGreater(after.price, START.price)
        back = quote_sell(after, tokens, 100)
        self.assertLess(back, 1.0)
        self.assertGreater(back, 0.95)  # ~1% fee each way plus small impact at this size

    def test_fee_is_applied(self):
        self.assertGreater(quote_buy(START, 1.0, 0), quote_buy(START, 1.0, 100))
        self.assertGreater(quote_sell(START, 1e6, 0), quote_sell(START, 1e6, 100))

    def test_graduation_flag(self):
        self.assertFalse(START.graduated)
        self.assertTrue(CurveState(115.0, 279_000_000).graduated)


class EventsIO(unittest.TestCase):
    def test_round_trip_every_event_type(self):
        events = [
            NewToken(1, "M", "C", "n", "S", 5.0, 0.1, 30.1, 1e9),
            Trade(2, "M", "T", "buy", 0.5, 100.0, 31.0, 9e8),
            ScanNote(3, "M", "PASS", 4, "x"),
        ]
        for ev in events:
            self.assertEqual(decode(encode(ev)), ev)

    def test_file_round_trip_and_bad_line_reports_line_number(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "e.jsonl")
            write_jsonl(path, [ScanNote(1, "M", "PASS")])
            self.assertEqual(len(read_jsonl(path)), 1)
            with open(path, "a") as f:
                f.write("\n{not json}\n")
            with self.assertRaisesRegex(ValueError, r":3: bad event line"):
                read_jsonl(path)


class PumpPortalParsing(unittest.TestCase):
    CREATE = {
        "signature": "sig", "mint": "MINT", "traderPublicKey": "CREATOR", "txType": "create",
        "initialBuy": 50_000_000.0, "solAmount": 1.5, "vSolInBondingCurve": 31.5, "vTokensInBondingCurve": 1.02e9,
        "name": "Cat", "symbol": "CAT", "pool": "pump",
    }

    def test_create(self):
        ev = parse_pumpportal(self.CREATE, 99)
        self.assertEqual((ev.mint, ev.creator, ev.t, ev.dev_tokens, ev.v_sol), ("MINT", "CREATOR", 99, 50_000_000.0, 31.5))

    def test_trade(self):
        msg = {**self.CREATE, "txType": "sell", "tokenAmount": 1000, "solAmount": 0.2}
        ev = parse_pumpportal(msg, 5)
        self.assertEqual((ev.side, ev.trader, ev.tokens, ev.sol), ("sell", "CREATOR", 1000.0, 0.2))

    def test_ignores_acks_other_pools_and_malformed(self):
        self.assertIsNone(parse_pumpportal({"message": "Successfully subscribed"}, 1))
        self.assertIsNone(parse_pumpportal({**self.CREATE, "pool": "bonk"}, 1))
        self.assertIsNone(parse_pumpportal({**self.CREATE, "vSolInBondingCurve": "abc"}, 1))
        broken = dict(self.CREATE)
        del broken["traderPublicKey"]
        self.assertIsNone(parse_pumpportal(broken, 1))
        self.assertIsNone(parse_pumpportal([1, 2], 1))
        self.assertIsNone(parse_pumpportal({**self.CREATE, "txType": "migrate"}, 1))


class OutcomeClassification(unittest.TestCase):
    def tracker(self, dev_sol=1.0):
        sim = Sim()
        launch = sim.create(dev_sol=dev_sol)
        return sim, launch, OutcomeTracker(launch)

    def test_creator_dump_and_collapse_is_a_rug(self):
        sim, launch, tr = self.tracker()
        tr.on_trade(sim.buy(2, 30.0))  # pump to ~4x launch price (price can't fall below launch price)
        tr.on_trade(sim.sell(3, launch.dev_tokens, "CREATOR"))  # dev dumps everything
        tr.on_trade(sim.sell(4, 5e8, "OTHER"))  # others follow
        o = tr.finalize()
        self.assertTrue(o.rugged, o)
        self.assertEqual(o.creator_sold_pct, 1.0)

    def test_creator_sells_into_strength_is_not_a_rug(self):
        sim, launch, tr = self.tracker()
        tr.on_trade(sim.buy(2, 5.0))
        tr.on_trade(sim.sell(3, launch.dev_tokens, "CREATOR"))
        self.assertFalse(tr.finalize().rugged)  # price is nowhere near 50% off its peak

    def test_abandonment_without_creator_sale_is_not_a_rug(self):
        sim, launch, tr = self.tracker()
        tr.on_trade(sim.buy(2, 5.0, "A"))
        tr.on_trade(sim.sell(3, 1.5e8, "A"))
        o = tr.finalize()
        self.assertFalse(o.rugged)
        self.assertEqual(o.creator_sold_pct, 0.0)

    def test_creator_with_no_buy_is_handled(self):
        sim, launch, tr = self.tracker(dev_sol=0.0)
        self.assertEqual(tr.finalize().creator_sold_pct, 0.0)


class Store(unittest.TestCase):
    def launch(self, mint, creator, t):
        return NewToken(t, mint, creator, "n", "S", 0.0, 0.0, 30.0, 1.073e9)

    def test_profile_counts_history_excluding_current_launch(self):
        s = LaunchStore()
        now = 10 * DAY_MS
        s.add_launch(self.launch("old", "C", now - 3 * DAY_MS))
        s.add_launch(self.launch("recent1", "C", now - 1000))
        s.add_launch(self.launch("recent2", "C", now - 2000))
        s.add_launch(self.launch("other", "D", now - 1000))
        s.add_launch(self.launch("current", "C", now))
        s.add_outcome("old", Outcome(True, 1.0, 100, 5, False))
        s.add_outcome("recent1", Outcome(False, 0.0, 100, 90, True))
        p = s.profile("C", now, exclude_mint="current")
        self.assertEqual((p.launches_total, p.launches_24h, p.rugs_total, p.graduated_total), (3, 2, 1, 1))

    def test_unknown_creator_has_empty_profile(self):
        p = LaunchStore().profile("nobody", 1)
        self.assertEqual((p.launches_total, p.rugs_total), (0, 0))

    def test_relaunch_of_same_mint_is_ignored(self):
        s = LaunchStore()
        s.add_launch(self.launch("m", "C", 1))
        s.add_launch(self.launch("m", "C", 2))
        self.assertEqual(s.profile("C", 3).launches_total, 1)


if __name__ == "__main__":
    unittest.main()
