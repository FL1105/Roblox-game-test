import json
import math
import threading
import unittest
import urllib.error
import urllib.request

from xrpl_arb.assets import Asset, amount_json, amount_value, fmt_iou, to_drops
from xrpl_arb.bot import Bot
from xrpl_arb.config import KNOWN, Config
from xrpl_arb.feed import DemoFeed, Snapshot, parse_offers, parse_pool
from xrpl_arb.liquidity import Hop, Offer, Pool, amm_out, amm_take_to_price
from xrpl_arb.paper import PaperExecutor
from xrpl_arb.plan import Result, run_plan
from xrpl_arb.routes import Route, evaluate, find_routes, optimize
from xrpl_arb.server import make_server
from xrpl_arb.state import BotState

XRP = Asset("XRP")
USD = Asset("USD", "rUSDissuer")
EUR = Asset("EUR", "rEURissuer")


def pool(a=XRP, b=USD, ra=1_000_000.0, rb=1_400_000.0, fee=0.002):
    return Pool(a, b, ra, rb, fee)


def snap_with(ledger=1, books=None, pools=None, rates=None):
    s = Snapshot(ledger, 0.0, 0.0, "test", rates=rates or {})
    s.books = books or {}
    s.pools = {p.key: p for p in (pools or [])}
    return s


def arb_snapshot(ledger, bid_price=1.45, bid_size=5000.0):
    """XRP/USD: the AMM sells XRP at ~1.40 USD, a book bid pays `bid_price`. Cycle XRP -> USD -> XRP wins by selling into the bid."""
    books = {(XRP.key, USD.key): [Offer(bid_size, bid_size * bid_price)] if bid_size else [], (USD.key, XRP.key): []}
    return snap_with(ledger, books, [pool()])


class Amounts(unittest.TestCase):
    def test_parse_and_keys(self):
        self.assertEqual(Asset.parse("XRP"), XRP)
        self.assertEqual(Asset.parse("USD+rABC"), Asset("USD", "rABC"))
        self.assertEqual(Asset.parse("USD+rABC").key, "USD+rABC")
        for bad in ("USD", "USD+abc", "+rABC"):
            with self.assertRaises(ValueError):
                Asset.parse(bad)

    def test_hex_currency_is_decoded(self):
        self.assertEqual(KNOWN["RLUSD"].code, "RLUSD")
        self.assertEqual(Asset("FF" * 20, "rX").code, "FFFFFFFF")  # not printable text: falls back to the hex prefix
        self.assertEqual(Asset("USD", "rX").code, "USD")

    def test_amount_values(self):
        self.assertEqual(amount_value("1500000"), 1.5)
        self.assertEqual(amount_value({"currency": "USD", "issuer": "r", "value": "2.25"}), 2.25)
        self.assertEqual(Asset.from_amount("5"), XRP)

    def test_exact_strings_round_down(self):
        self.assertEqual(to_drops(1.2345678), "1234567")
        self.assertEqual(fmt_iou(0.1 + 0.2), "0.3")  # 0.30000000000000004 -> 15 significant digits, rounded down
        self.assertEqual(fmt_iou(123456789.123456789), "123456789.123456")
        self.assertEqual(amount_json(XRP, 2.0), "2000000")
        self.assertEqual(amount_json(USD, 2.5), {"currency": "USD", "issuer": "rUSDissuer", "value": "2.5"})
        self.assertLessEqual(float(fmt_iou(1 / 3)), 1 / 3)


class AmmMath(unittest.TestCase):
    def test_swap_matches_constant_product(self):
        x, y, f = 1000.0, 2000.0, 0.01
        out = amm_out(x, y, f, 100.0)
        eff = 100.0 * (1 - f)
        self.assertAlmostEqual(out, y - x * y / (x + eff), places=9)

    def test_take_to_price_lands_on_the_target_price(self):
        x, y, f, target = 1000.0, 2000.0, 0.003, 0.52  # pool quotes 0.5/(1-f); target is worse
        t = amm_take_to_price(x, y, f, target)
        self.assertGreater(t, 0)
        x2, y2 = x + t, y - amm_out(x, y, f, t)
        self.assertAlmostEqual(x2 / ((1 - f) * y2), target, places=9)

    def test_take_to_price_is_zero_when_pool_is_already_worse(self):
        self.assertEqual(amm_take_to_price(1000.0, 2000.0, 0.003, 0.4), 0.0)


class HopQuotes(unittest.TestCase):
    def test_amm_only_equals_one_swap(self):
        p = pool()
        q = Hop(XRP, USD, [], p).quote(5000.0)
        self.assertAlmostEqual(q.out, amm_out(p.ra, p.rb, p.fee, 5000.0), places=6)
        self.assertEqual((q.clob_in, q.amm_share), (0.0, 1.0))

    def test_book_only_walks_offers_best_first(self):
        offers = [Offer(100, 50), Offer(210, 100)]  # prices 2.0 then 2.1 paid per unit received
        q = Hop(XRP, USD, offers, None).quote(150.0)
        self.assertAlmostEqual(q.out, 50 + 50 / 2.1, places=9)
        self.assertAlmostEqual(q.spent, 150.0, places=9)
        self.assertFalse(q.exhausted)

    def test_book_only_runs_dry(self):
        q = Hop(XRP, USD, [Offer(100, 50)], None).quote(150.0)
        self.assertTrue(q.exhausted)
        self.assertAlmostEqual(q.spent, 100.0)
        self.assertAlmostEqual(q.out, 50.0)

    def test_merged_is_at_least_as_good_as_either_venue_alone(self):
        p = pool(rb=1_000_000.0)  # AMM ask near 1.0 XRP per USD
        offers = [Offer(1000 * 0.9995, 1000), Offer(2000 * 1.002, 2000), Offer(5000 * 1.01, 5000)]
        for size in (100, 900, 3000, 20_000, 200_000):
            merged = Hop(XRP, USD, offers, p).quote(size).out
            self.assertGreaterEqual(merged + 1e-9, Hop(XRP, USD, offers, None).quote(size).out if size <= 8000 else 0)
            self.assertGreaterEqual(merged + 1e-9, Hop(XRP, USD, [], p).quote(size).out)

    def test_output_is_increasing_and_concave_in_input(self):
        p = pool(rb=1_000_000.0)
        offers = [Offer(1000 * 0.9995, 1000), Offer(2000 * 1.002, 2000)]
        hop = Hop(XRP, USD, offers, p)
        sizes = [i * 250.0 for i in range(1, 41)]
        outs = [hop.quote(s).out for s in sizes]
        slopes = [(b - a) / 250.0 for a, b in zip(outs, outs[1:])]
        self.assertTrue(all(o2 > o1 for o1, o2 in zip(outs, outs[1:])))
        self.assertTrue(all(s2 <= s1 + 1e-9 for s1, s2 in zip(slopes, slopes[1:])))

    def test_cheaper_book_offer_is_taken_before_the_pool(self):
        p = pool(rb=1_000_000.0)  # AMM ask ~1.002/(1) -> well above the offer below
        q = Hop(XRP, USD, [Offer(500 * 0.99, 500)], p).quote(300.0)
        self.assertEqual(q.amm_in, 0.0)
        self.assertAlmostEqual(q.out, 300 / 0.99, places=9)

    def test_transfer_fee_is_paid_on_both_sides(self):
        offers = [Offer(100, 100)]
        base = Hop(USD, EUR, offers, None).quote(10.0).out
        self.assertAlmostEqual(Hop(USD, EUR, offers, None, rate_in=1.002).quote(10.0).out, base / 1.002, places=9)
        self.assertAlmostEqual(Hop(USD, EUR, offers, None, rate_out=1.002).quote(10.0).out, base / 1.002, places=9)

    def test_pool_state_carries_between_hops_of_one_evaluation(self):
        p = pool()
        pools = {p.key: [p.ra, p.rb]}
        buy, sell = Hop(XRP, USD, [], p), Hop(USD, XRP, [], p)
        usd = buy.quote(10_000.0, pools).out
        back = sell.quote(usd, pools).out
        self.assertLess(back, 10_000.0 * (1 - p.fee))  # a round trip through one pool loses at least a full fee (the second swap sees the first one's price impact)
        self.assertGreater(back, 9_900.0)
        self.assertNotEqual(pools[p.key], [p.ra, p.rb])

    def test_quote_without_scratch_state_does_not_mutate_the_pool(self):
        p = pool()
        Hop(XRP, USD, [], p).quote(10_000.0)
        self.assertEqual((p.ra, p.rb), (1_000_000.0, 1_400_000.0))


class Parsing(unittest.TestCase):
    def test_offers_use_funded_amounts_and_skip_our_own(self):
        rows = [
            {"Account": "rA", "TakerGets": {"currency": "USD", "issuer": "i", "value": "100"}, "TakerPays": "50000000"},
            {"Account": "rB", "TakerGets": {"currency": "USD", "issuer": "i", "value": "100"}, "TakerPays": "40000000",
             "taker_gets_funded": {"currency": "USD", "issuer": "i", "value": "10"}, "taker_pays_funded": "4000000"},
            {"Account": "rMe", "TakerGets": {"currency": "USD", "issuer": "i", "value": "1"}, "TakerPays": "1000000"},
        ]
        offers = parse_offers(rows, skip_owner="rMe")
        self.assertEqual([o.owner for o in offers], ["rB", "rA"])  # best price first
        self.assertEqual((offers[0].inp, offers[0].out), (4.0, 10.0))

    def test_pool_parses_in_either_order_and_scales_fee(self):
        usd = {"currency": "USD", "issuer": "rUSDissuer", "value": "2000"}
        info = {"amm": {"amount": "1000000000", "amount2": usd, "trading_fee": 205}}
        p = parse_pool(info, XRP, USD)
        self.assertEqual((p.ra, p.rb, p.fee), (1000.0, 2000.0, 0.00205))
        swapped = {"amm": {"amount": usd, "amount2": "1000000000", "trading_fee": 205}}
        q = parse_pool(swapped, XRP, USD)
        self.assertEqual((q.ra, q.rb), (1000.0, 2000.0))

    def test_frozen_or_empty_pools_are_ignored(self):
        usd = {"currency": "USD", "issuer": "rUSDissuer", "value": "2000"}
        self.assertIsNone(parse_pool({"amm": {"amount": "1000000000", "amount2": usd, "asset2_frozen": True}}, XRP, USD))
        self.assertIsNone(parse_pool({"amm": {"amount": "0", "amount2": usd}}, XRP, USD))
        self.assertIsNone(parse_pool({}, XRP, USD))


class Routes(unittest.TestCase):
    def test_cycle_enumeration(self):
        markets = [(XRP, USD), (USD, EUR), (EUR, XRP)]
        ids = {r.id for r in find_routes(XRP, markets, 3)}
        self.assertEqual(ids, {f"{a}>{b}>XRP" for a, b in ((XRP.key, USD.key), (XRP.key, EUR.key))} | {
            f"XRP>{USD.key}>{EUR.key}>XRP", f"XRP>{EUR.key}>{USD.key}>XRP"})

    def test_two_hop_cycles_only_when_max_hops_is_two(self):
        routes = find_routes(XRP, [(XRP, USD), (USD, EUR), (EUR, XRP)], 2)
        self.assertEqual({r.hops for r in routes}, {2})

    def test_no_route_revisits_an_asset(self):
        for r in find_routes(XRP, [(XRP, USD), (USD, EUR), (EUR, XRP), (XRP, EUR)], 4):
            mids = r.assets[1:-1]
            self.assertEqual(len(mids), len(set(mids)))

    def _round_trip(self, snap, size):
        route = Route((XRP, USD, XRP))
        hops = [snap.hop(XRP, USD), snap.hop(USD, XRP)]
        return route, hops, evaluate(route, hops, snap.fresh_pools(), size)

    def test_evaluate_finds_the_book_vs_pool_gap(self):
        _, _, ev = self._round_trip(arb_snapshot(1), 1000.0)
        self.assertTrue(ev.complete)
        self.assertGreater(ev.profit, 25.0)  # ~3% of 1000 XRP
        self.assertEqual(ev.legs[0].amm_share, 0.0)  # leg 1 sells into the book
        self.assertEqual(ev.legs[1].amm_share, 1.0)  # leg 2 buys back from the pool

    def test_no_gap_means_a_loss(self):
        s = arb_snapshot(1, bid_price=1.40 * 0.99)
        self.assertLess(self._round_trip(s, 1000.0)[2].profit, 0)

    def test_optimize_matches_a_brute_force_search(self):
        s = arb_snapshot(1, bid_size=800.0)  # the bid only has 800 XRP of room, so the best size is bounded
        route, hops, _ = self._round_trip(s, 100.0)
        best = optimize(route, hops, s.fresh_pools, 10.0, 5000.0)
        grid = max(evaluate(route, hops, s.fresh_pools(), 10.0 + i * 4.99).profit for i in range(1000))
        self.assertGreaterEqual(best.profit, grid - 1e-6)
        self.assertGreater(best.profit, 0)

    def test_optimize_reports_the_loss_when_nothing_pays(self):
        s = arb_snapshot(1, bid_price=1.0)
        route, hops, _ = self._round_trip(s, 100.0)
        ev = optimize(route, hops, s.fresh_pools, 10.0, 1000.0)
        self.assertLess(ev.profit, 0)
        self.assertEqual(ev.size, 10.0)

    def test_thin_books_are_flagged_incomplete(self):
        s = snap_with(1, {(XRP.key, USD.key): [Offer(10, 14)], (USD.key, XRP.key): [Offer(14, 10)]})
        route, hops, ev = self._round_trip(s, 100.0)
        self.assertFalse(ev.complete)


class Policy(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(min_profit_bps=10, min_profit_base=0.0, slip_bps=10, max_loss_bps=100)
        snap = arb_snapshot(1)
        route = Route((XRP, USD, XRP))
        self.ev = evaluate(route, [snap.hop(XRP, USD), snap.hop(USD, XRP)], snap.fresh_pools(), 1000.0)

    def drive(self, *results):
        gen = run_plan(self.ev, self.cfg)
        a = next(gen)
        seen = [a]
        try:
            for r in results:
                a = gen.send(r)
                seen.append(a)
        except StopIteration as stop:
            return stop.value, seen
        raise AssertionError("plan did not finish")

    def test_happy_path(self):
        out, seen = self.drive(Result(True, self.ev.legs[0].amount_out), Result(True, self.ev.out))
        self.assertEqual(out.status, "WON")
        self.assertAlmostEqual(out.realized, self.ev.profit + self.ev.fee_cost, places=9)
        self.assertAlmostEqual(seen[0].min_out, self.ev.legs[0].amount_out * 0.999)
        self.assertGreater(seen[1].min_out, 1000.0)  # the last leg must return at least the stake plus the threshold

    def test_first_leg_failing_costs_nothing(self):
        out, _ = self.drive(Result(False, note="would fill -1% vs quote"))
        self.assertEqual((out.status, out.realized), ("MISSED", 0.0))

    def test_unwind_after_profit_disappears(self):
        out, seen = self.drive(Result(True, 1400.0), Result(False), Result(True, 995.0))
        self.assertEqual((out.status, seen[-1].kind), ("LOST", "unwind"))
        self.assertAlmostEqual(out.realized, -5.0)
        self.assertAlmostEqual(seen[-1].min_out, 990.0)

    def test_unwind_that_still_nets_a_gain_counts_as_won(self):
        out, _ = self.drive(Result(True, 1400.0), Result(False), Result(True, 1001.0))
        self.assertEqual(out.status, "WON")

    def test_stuck_when_even_the_unwind_fails(self):
        out, _ = self.drive(Result(True, 1400.0), Result(False), Result(False))
        self.assertEqual(out.status, "STUCK")
        self.assertEqual(out.holding, (1, 1400.0))


class Paper(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(markets=(("XRP", "USD.Bitstamp"),), min_profit_bps=10, min_profit_base=0.0, cooldown_ledgers=3, size_min=100.0, size_max=1000.0)
        self.state = BotState(self.cfg, "paper", "test")
        self.ex = PaperExecutor(XRP, self.state)
        snap = arb_snapshot(10)
        self.route = Route((XRP, USD, XRP))
        self.ev = optimize(self.route, [snap.hop(XRP, USD), snap.hop(USD, XRP)], snap.fresh_pools, 100.0, 1000.0)
        self.ex.open(self.ev, snap)

    def test_opening_records_the_quote_and_a_cooldown(self):
        self.assertTrue(self.ex.busy)
        self.assertEqual(self.state.stats["opened"], 1)
        self.assertAlmostEqual(self.state.stats["pnl_theory"], self.ev.profit)
        self.assertTrue(self.ex.on_cooldown(self.route.id, 11))
        self.assertFalse(self.ex.on_cooldown(self.route.id, 13))

    def test_legs_land_on_successive_ledgers(self):
        self.ex.on_snapshot(arb_snapshot(10))  # same ledger: nothing happens
        self.assertTrue(self.ex.busy)
        self.ex.on_snapshot(arb_snapshot(11))  # leg 1
        self.assertTrue(self.ex.busy)
        self.assertEqual(self.state.stats["won"], 0)
        self.ex.on_snapshot(arb_snapshot(12))  # leg 2
        self.assertFalse(self.ex.busy)
        self.assertEqual(self.state.stats["won"], 1)
        self.assertGreater(self.state.stats["pnl_real"], 0)
        self.assertAlmostEqual(self.state.stats["pnl_real"], self.ev.profit, delta=0.01 * self.ev.profit)

    def test_opportunity_gone_by_the_first_leg_is_a_miss(self):
        self.ex.on_snapshot(arb_snapshot(11, bid_price=1.40 * 0.99))
        self.assertFalse(self.ex.busy)
        self.assertEqual((self.state.stats["missed"], self.state.stats["pnl_real"]), (1, 0.0))
        self.assertEqual(self.state.trades[0]["status"], "MISSED")

    def test_profit_gone_by_the_last_leg_is_unwound_at_a_loss(self):
        self.ex.on_snapshot(arb_snapshot(11))
        self.ex.on_snapshot(arb_snapshot(12, bid_price=1.40 * 0.999))  # pool now dearer than the book can pay: round trip loses
        self.assertEqual(self.state.stats["lost"] + self.state.stats["won"], 1)
        self.assertLess(self.state.stats["pnl_real"], self.ev.profit)

    def test_a_trade_that_cannot_be_unwound_halts_the_bot(self):
        self.ex.on_snapshot(arb_snapshot(11))
        broken = snap_with(12, {(XRP.key, USD.key): [], (USD.key, XRP.key): [Offer(1.0, 0.5)]}, [pool(ra=1.0, rb=1.0)])  # nothing left to sell into
        self.ex.on_snapshot(broken)
        self.assertEqual(self.state.stats["stuck"], 1)
        self.assertIsNotNone(self.state.halted)
        self.assertFalse(self.state.running)


class Settings(unittest.TestCase):
    def test_update_changes_only_what_was_sent(self):
        cfg = Config()
        new = cfg.update({"min_profit_bps": 25, "cooldown_ledgers": 7})
        self.assertEqual((new.min_profit_bps, new.cooldown_ledgers, new.size_max), (25.0, 7, cfg.size_max))

    def test_rejects_bad_input(self):
        cfg = Config()
        bad = [{"min_profit_bps": -1}, {"min_profit_bps": True}, {"min_profit_bps": "10"}, {"min_profit_bps": math.nan}, {"min_profit_bps": math.inf},
               {"cooldown_ledgers": 1.5}, {"size_min": 1000, "size_max": 10}, {"nonsense": 1}, {"base": "RLUSD"}, {"slip_bps": 9999}]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ValueError):
                cfg.update(b)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            Config(base="NOPE").validate()
        with self.assertRaises(ValueError):
            Config(base="EUR.GateHub", markets=(("XRP", "RLUSD"),)).validate()  # not in any watched market
        with self.assertRaises(ValueError):
            Config(max_hops=9).validate()
        self.assertEqual(Config().validate().base, "XRP")


class BotLoop(unittest.TestCase):
    def test_demo_run_publishes_state_and_trades(self):
        cfg = Config()
        state = BotState(cfg, "paper", "demo")
        feed = DemoFeed(cfg, seed=7, interval=0)
        bot = Bot(cfg, feed, state, PaperExecutor(cfg.assets()[cfg.base], state))
        for _ in range(120):
            idx, close = feed.latest()
            bot.step(feed.snapshot(idx, close))
        s = state.snapshot()
        json.dumps(s)  # everything the dashboard receives must be plain JSON
        self.assertEqual(s["stats"]["ledgers"], 120)
        self.assertEqual(len(s["series"]), 120)
        self.assertEqual(len(s["routes"]), len(bot.routes))
        self.assertEqual(len(s["markets"]), len(cfg.markets))
        st = s["stats"]
        self.assertGreater(st["opened"], 0)
        self.assertEqual(st["won"] + st["lost"] + st["missed"] + st["stuck"] + (1 if bot.executor.busy else 0), st["opened"])
        self.assertTrue(all(r["status"] in {"EXECUTABLE", "BELOW THRESHOLD", "NO EDGE", "THIN BOOK", "COOLDOWN", "NO DATA", "NOT TRADABLE"} for r in s["routes"]))

    def test_stop_switch_keeps_scanning_but_opens_nothing(self):
        cfg = Config()
        state = BotState(cfg, "paper", "demo")
        feed = DemoFeed(cfg, seed=7, interval=0)
        bot = Bot(cfg, feed, state, PaperExecutor(cfg.assets()[cfg.base], state))
        state.set_running(False)
        for _ in range(60):
            idx, close = feed.latest()
            bot.step(feed.snapshot(idx, close))
        self.assertEqual(state.stats["opened"], 0)
        self.assertGreater(state.stats["executable"], 0)

    def test_missing_market_data_marks_the_route_instead_of_crashing(self):
        cfg = Config(markets=(("XRP", "RLUSD"),))
        state = BotState(cfg, "paper", "test")
        bot = Bot(cfg, DemoFeed(cfg), state, PaperExecutor(cfg.assets()[cfg.base], state))
        bot.step(snap_with(5))
        self.assertEqual({r["status"] for r in state.routes}, {"NO DATA"})


class LiveFeedReads(unittest.TestCase):
    class Stub:
        def __init__(self):
            self.calls = []

        def call(self, method, **p):
            from xrpl_arb.rpc import RpcError

            self.calls.append(method)
            if method == "account_info":
                return {"account_data": {"TransferRate": 1002000000}}
            if method == "book_offers":
                return {"offers": [{"Account": "rM", "TakerGets": "2000000", "TakerPays": {"currency": "USD", "issuer": "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", "value": "3"}}]}
            raise RpcError("actNotFound")

    def test_snapshot_reads_books_rates_and_remembers_missing_pools(self):
        from xrpl_arb.feed import LiveFeed

        cfg = Config(markets=(("XRP", "USD.Bitstamp"),))
        rpc = self.Stub()
        feed = LiveFeed(cfg, rpc)
        try:
            snap = feed.snapshot(5, 0.0)
            self.assertEqual(snap.rates[KNOWN["USD.Bitstamp"].key], 1.002)
            self.assertEqual(len(snap.books), 2)
            self.assertEqual(snap.pools, {})
            feed.snapshot(6, 0.0)
        finally:
            feed.close()
        self.assertEqual(rpc.calls.count("amm_info"), 1)  # not asked again on the next ledger
        self.assertEqual(rpc.calls.count("book_offers"), 4)
        self.assertEqual(rpc.calls.count("account_info"), 1)  # transfer rates are cached too

    def test_a_failed_read_marks_the_pair_instead_of_guessing(self):
        from xrpl_arb.feed import LiveFeed

        class Down:
            def call(self, method, **p):
                if method == "account_info":
                    return {"account_data": {}}
                raise OSError("down")

        cfg = Config(markets=(("XRP", "RLUSD"),))
        feed = LiveFeed(cfg, Down())
        try:
            snap = feed.snapshot(5, 0.0)
        finally:
            feed.close()
        self.assertIsNone(snap.hop(XRP, KNOWN["RLUSD"]))


class RpcFailover(unittest.TestCase):
    """Real local HTTP servers standing in for XRPL nodes."""

    def serve(self, handler):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                code, body = handler()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def setUp(self):
        import xrpl_arb.rpc as rpc_mod

        self.rpc_mod = rpc_mod
        self._pause = rpc_mod.RETRY_PAUSE_S
        rpc_mod.RETRY_PAUSE_S = 0.0
        self.addCleanup(setattr, rpc_mod, "RETRY_PAUSE_S", self._pause)

    def ok(self, **result):
        return lambda: (200, json.dumps({"result": {"status": "success", **result}}).encode())

    def test_falls_over_to_the_next_server_and_remembers(self):
        bad = self.serve(lambda: (500, b"nope"))
        hits = []
        good = self.serve(lambda: (hits.append(1), self.ok(x=1)())[1])
        rpc = self.rpc_mod.Rpc((bad, good))
        self.assertEqual(rpc.call("ping")["x"], 1)
        self.assertEqual(rpc.url, good)  # the failed server is put at the back for a while
        self.assertEqual(rpc.call("ping")["x"], 1)

    def test_a_momentary_failure_is_retried_on_the_same_server(self):
        n = []

        def flaky():
            n.append(1)
            return (500, b"x") if len(n) == 1 else self.ok(x=2)()
        only = self.serve(flaky)
        self.assertEqual(self.rpc_mod.Rpc((only,)).call("ping")["x"], 2)

    def test_ledger_errors_are_raised_not_retried(self):
        calls = []
        srv = self.serve(lambda: (calls.append(1), (200, json.dumps({"result": {"status": "error", "error": "actNotFound"}}).encode()))[1])
        with self.assertRaises(self.rpc_mod.RpcError) as cm:
            self.rpc_mod.Rpc((srv,)).call("account_info")
        self.assertEqual((cm.exception.code, len(calls)), ("actNotFound", 1))

    def test_busy_servers_count_as_failures_and_all_failing_is_an_oserror(self):
        busy = self.serve(lambda: (200, json.dumps({"result": {"status": "error", "error": "tooBusy"}}).encode()))
        with self.assertRaises(OSError):
            self.rpc_mod.Rpc((busy,)).call("ping")


class History(unittest.TestCase):
    def test_windows_fit_inside_the_range(self):
        from xrpl_arb.history import plan_windows

        starts = plan_windows(1000, 1100, 30, 5)
        self.assertEqual(starts, [1000, 1030, 1060, 1090])
        self.assertTrue(all(s + 4 <= 1100 for s in starts))
        self.assertEqual(plan_windows(1000, 1003, 30, 5), [])

    def test_percentile(self):
        from xrpl_arb.history import pct

        self.assertEqual(pct([5, 1, 3, 2, 4], 0.5), 3)
        self.assertEqual(pct([5, 1, 3, 2, 4], 0.99), 5)
        self.assertTrue(math.isnan(pct([], 0.5)))

    def replay(self, feed_cls=DemoFeed, **kw):
        from xrpl_arb.history import plan_windows, replay, summarize

        cfg = Config()
        feed = feed_cls(cfg, seed=11, interval=0)
        starts = plan_windows(5000, 5000 + 400, 5, 5)
        samples, state = replay(cfg, feed, starts, 5, workers=1, **kw)
        return cfg, starts, samples, state, summarize

    def test_every_trade_opened_in_a_window_finishes_in_it(self):
        cfg, starts, samples, state, _ = self.replay()
        st = state.stats
        self.assertGreater(st["opened"], 0)  # the synthetic market has gaps, so there is something to trade
        self.assertEqual(st["opened"], st["won"] + st["lost"] + st["missed"] + st["stuck"])
        self.assertEqual(len(samples), len(starts) * 5 * 10)  # ten cycles priced at each of five ledgers in every window

    def test_trades_only_open_early_enough_to_finish(self):
        cfg, starts, samples, state, _ = self.replay()
        opened_at = {int(t["ledger"]) for t in state.trades}
        self.assertTrue(all((led - 5000) % 5 <= 5 - 1 - cfg.max_hops for led in opened_at))

    def test_summary_shape(self):
        cfg, starts, samples, state, summarize = self.replay()
        rep = summarize(samples, state.stats, starts, 0.0, 1.0, 5, 5)
        json.dumps(rep)
        self.assertEqual(rep["windows"], len(starts))
        self.assertEqual(len(rep["routes"]), 10)
        p90 = [r["edge_bps_p90"] for r in rep["routes"]]
        self.assertEqual(p90, sorted(p90, reverse=True))
        for r in rep["routes"]:
            self.assertTrue(0 <= r["share_executable"] <= r["share_profitable"] <= 1)
        self.assertEqual(rep["paper"]["opened"], state.stats["opened"])

    def test_windows_that_cannot_be_read_are_skipped_not_fatal(self):
        class Flaky(DemoFeed):
            def snapshot(self, ledger, close_time):
                if (ledger - 5000) // 5 % 2 == 0:
                    raise OSError("history unavailable")
                return super().snapshot(ledger, close_time)

        cfg, starts, samples, state, _ = self.replay(Flaky)
        self.assertEqual(state.stats["windows_skipped"], len([s for s in starts if (s - 5000) // 5 % 2 == 0]))
        self.assertEqual(len(samples), (len(starts) - state.stats["windows_skipped"]) * 5 * 10)

    def test_cli_rejects_windows_too_short_for_the_longest_trade(self):
        from xrpl_arb.history import main

        with self.assertRaises(SystemExit) as cm:
            main(["--window", "3"])
        self.assertIn("--window", str(cm.exception))
        with self.assertRaises(SystemExit) as cm:
            main(["--markets", "XRPRLUSD"])
        self.assertIn("--markets", str(cm.exception))


class BotResilience(unittest.TestCase):
    def test_an_unexpected_error_is_reported_and_the_loop_keeps_going(self):
        import contextlib
        import io

        cfg = Config(poll_s=0.0)
        state = BotState(cfg, "paper", "demo")
        inner = DemoFeed(cfg, seed=3, interval=0)
        calls = []

        class Flaky:
            def latest(self):
                calls.append(1)
                if len(calls) == 1:
                    raise ZeroDivisionError("bug")
                return inner.latest()

            def snapshot(self, i, c):
                return inner.snapshot(i, c)

        bot = Bot(cfg, Flaky(), state, PaperExecutor(cfg.assets()[cfg.base], state))
        stop = threading.Event()
        with contextlib.redirect_stderr(io.StringIO()):
            t = threading.Thread(target=bot.run, args=(stop,), daemon=True)
            t.start()
            for _ in range(200):
                if state.stats["ledgers"] >= 2:
                    break
                state.wait(state.version, 0.2)
            stop.set()
            t.join(5)
        self.assertGreaterEqual(state.stats["ledgers"], 2)
        self.assertTrue(any(e["level"] == "error" and "ZeroDivisionError" in e["msg"] for e in state.events))

    def test_routes_skipped_for_lack_of_funds_say_so(self):
        cfg = Config(markets=(("XRP", "RLUSD"),))
        state = BotState(cfg, "paper", "test")
        feed = DemoFeed(cfg, seed=1, interval=0)
        bot = Bot(cfg, feed, state, PaperExecutor(cfg.assets()[cfg.base], state), size_cap=lambda: 1.0)
        idx, close = feed.latest()
        bot.step(feed.snapshot(idx, close))
        self.assertEqual({r["status"] for r in state.routes}, {"NOT TRADABLE"})
        self.assertIn("less than the smallest trade", state.routes[0]["note"])


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = BotState(Config(), "paper", "demo")
        cls.srv = make_server(cls.state, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, data=None, ctype="application/json", host=None, raw=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method="POST" if body is not None else "GET")
        if body is not None:
            r.add_header("Content-Type", ctype)
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_page_and_state(self):
        code, body, headers = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"XRPL Arb Console", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        code, body, _ = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["mode"], "paper")

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.req("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.req("/api/settings", {"min_profit_bps": 5}, host="evil.example")[0], 403)

    def test_posts_must_be_json_objects(self):
        self.assertEqual(self.req("/api/bot", {"running": True}, ctype="text/plain")[0], 415)
        self.assertEqual(self.req("/api/bot", raw=b"[1]")[0], 400)
        self.assertEqual(self.req("/api/bot", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("/api/bot", {"running": "yes"})[0], 400)

    def test_start_stop_and_settings(self):
        self.assertEqual(json.loads(self.req("/api/bot", {"running": False})[1]), {"running": False})
        self.assertFalse(self.state.running)
        self.req("/api/bot", {"running": True})
        self.assertTrue(self.state.running)
        code, body, _ = self.req("/api/settings", {"min_profit_bps": 33, "size_max": 250})
        self.assertEqual(code, 200)
        self.assertEqual((self.state.cfg.min_profit_bps, self.state.cfg.size_max), (33.0, 250.0))
        code, body, _ = self.req("/api/settings", {"min_profit_bps": -5})
        self.assertEqual(code, 400)
        self.assertEqual(self.state.cfg.min_profit_bps, 33.0)  # a rejected change changes nothing

    def test_unknown_paths(self):
        self.assertEqual(self.req("/nope")[0], 404)
        self.assertEqual(self.req("/api/nope", {})[0], 404)

    def test_stream_sends_a_snapshot(self):
        r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/stream", timeout=5)
        try:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            line = r.readline()
            self.assertTrue(line.startswith(b"data: "))
            self.assertEqual(json.loads(line[6:])["mode"], "paper")
        finally:
            r.close()


class Page(unittest.TestCase):
    def test_page_has_no_external_requests_and_no_innerhtml(self):
        from xrpl_arb.server import INDEX

        html = INDEX.read_text()
        self.assertNotIn("innerHTML", html)  # chain and API strings are rendered with text nodes only
        self.assertNotIn("http://", html.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", html)
        self.assertNotRegex(html, r"<script[^>]+src=")
        for word in ("seed", "secret", "private key"):
            self.assertNotIn(word, html.lower())


try:
    import xrpl  # noqa: F401
    HAVE_XRPL_PY = True
except ImportError:
    HAVE_XRPL_PY = False


class FakeRpc:
    """Scripted stand-in for the XRPL server, enough for the live executor."""

    def __init__(self, address, sim_result="tesSUCCESS", sim_delivered="500", tx_result="tesSUCCESS", xrp="1000000000"):
        self.address, self.calls = address, []
        self.sim_result, self.sim_delivered, self.tx_result, self.xrp = sim_result, sim_delivered, tx_result, xrp
        self.submitted = []
        self.url = "fake"

    def call(self, method, **p):
        self.calls.append(method)
        if method == "account_info":
            idx = p.get("ledger_index")
            r = {"account_data": {"Balance": self.xrp, "OwnerCount": 2, "Sequence": 77}}
            return {**r, "ledger_current_index": 1000} if idx == "current" else r
        if method == "server_info":
            return {"info": {"validated_ledger": {"reserve_base_xrp": 1, "reserve_inc_xrp": 0.2}}}
        if method == "account_lines":
            return {"lines": [{"currency": "USD", "balance": "250"}] if p.get("peer") == USD.issuer else []}
        if method == "simulate":
            # a good-looking delivered amount even when the result is a failure, so only the engine_result check can stop the send
            return {"engine_result": self.sim_result, "meta": {"delivered_amount": self.sim_delivered}}
        if method == "fee":
            return {"drops": {"base_fee": "10", "open_ledger_fee": "12"}}
        if method == "submit":
            self.submitted.append(p["tx_blob"])
            return {"engine_result": "tesSUCCESS"}
        if method == "tx":
            return {"validated": True, "meta": {"TransactionResult": self.tx_result, "delivered_amount": self.sim_delivered}}
        if method == "ledger":
            return {"ledger": {"ledger_index": "1001"}}
        raise AssertionError(f"unexpected call {method}")


@unittest.skipUnless(HAVE_XRPL_PY, "xrpl-py not installed")
class Live(unittest.TestCase):
    def setUp(self):
        from xrpl.wallet import Wallet

        self.wallet = Wallet.create()
        self.seed = self.wallet.seed
        self.cfg = Config(markets=(("XRP", "USD.Bitstamp"),), size_min=10.0, size_max=100.0)
        self.cfg_assets = self.cfg.assets()
        # a two-leg plan: XRP -> USD.Bitstamp -> XRP, quoted at 100 XRP in / 103 out
        self.usd = self.cfg_assets["USD.Bitstamp"]
        from xrpl_arb.routes import Evaluation, Leg

        route = Route((XRP, self.usd, XRP))
        self.ev = Evaluation(route, 100.0, 103.0, [Leg(XRP, self.usd, 100.0, 140.0, 0.0), Leg(self.usd, XRP, 140.0, 103.0, 1.0)])
        self.state = BotState(self.cfg, "live", "xrpl")
        self.state.names = {a.key: n for n, a in KNOWN.items()}

    def make(self, rpc=None, **kw):
        from xrpl_arb.live import LiveExecutor

        rpc = rpc or FakeRpc(self.wallet.classic_address)
        # the fake wallet holds a trust line for USD.Bitstamp through the issuer match below
        orig = rpc.call

        def call(method, **p):
            if method == "account_lines" and p.get("peer") == self.usd.issuer:
                rpc.calls.append(method)
                return {"lines": [{"currency": "USD", "balance": "250"}]}
            return orig(method, **p)
        rpc.call = call
        return LiveExecutor(self.cfg, XRP, self.state, rpc, self.seed, **kw), rpc

    def test_bad_seed_is_refused_without_echoing_it(self):
        from xrpl_arb.live import LiveExecutor, LiveSetupError

        with self.assertRaises(LiveSetupError) as cm:
            LiveExecutor(self.cfg, XRP, self.state, FakeRpc("x"), "not-a-seed-1234")
        self.assertNotIn("not-a-seed-1234", str(cm.exception))

    def test_seed_never_reaches_the_dashboard_state(self):
        ex, _ = self.make()
        blob = json.dumps(self.state.snapshot())
        self.assertNotIn(self.seed, blob)
        self.assertEqual(self.state.snapshot()["account"]["address"], self.wallet.classic_address)
        self.assertNotIn(self.seed, repr(ex.__dict__.get("state", "")))

    def test_spendable_leaves_the_reserve_and_a_buffer(self):
        ex, _ = self.make()
        self.assertAlmostEqual(ex.spendable_base(), 1000.0 - (1 + 2 * 0.2) - 2.0)

    def test_missing_trust_line_blocks_the_route(self):
        ex, _ = self.make()
        other = Route((XRP, Asset("EUR", "rNoLine"), XRP))
        self.assertIn("no trust line", ex.can_trade(other))
        self.assertIsNone(ex.can_trade(self.ev.route))

    def test_payment_fields(self):
        ex, _ = self.make()
        buy = ex.payment_json(XRP, self.usd, 100.0, 139.0, 140.0)
        self.assertEqual(buy["SendMax"], "100000000")
        self.assertEqual(buy["DeliverMin"], {"currency": "USD", "issuer": self.usd.issuer, "value": "139"})
        self.assertEqual(buy["Flags"], 0x00020000)
        self.assertEqual(buy["Account"], buy["Destination"])
        self.assertGreater(float(buy["Amount"]["value"]), 140.0)  # a ceiling, not a target
        sell = ex.payment_json(self.usd, XRP, 140.0, 101.0, 103.0)
        self.assertEqual(sell["DeliverMin"], "101000000")
        self.assertEqual(sell["SendMax"]["value"], "140")

    def test_attempt_submits_only_after_a_good_simulation(self):
        from xrpl_arb.plan import Attempt

        ex, rpc = self.make(FakeRpc("", sim_delivered={"currency": "USD", "issuer": USD.issuer, "value": "140"}))
        res = ex._attempt(self.ev, Attempt(0, 100.0, 139.0, "leg"))
        self.assertTrue(res.ok)
        self.assertEqual(res.out, 140.0)
        self.assertEqual(len(rpc.submitted), 1)
        self.assertLess(rpc.calls.index("simulate"), rpc.calls.index("submit"))
        self.assertEqual(ex._fee_drops, 12)

    def test_failed_simulation_sends_nothing(self):
        from xrpl_arb.plan import Attempt

        ex, rpc = self.make(FakeRpc("", sim_result="tecPATH_PARTIAL", sim_delivered={"currency": "USD", "issuer": USD.issuer, "value": "140"}))
        res = ex._attempt(self.ev, Attempt(0, 100.0, 139.0, "leg"))
        self.assertFalse(res.ok)
        self.assertFalse(res.sent)
        self.assertIn("tecPATH_PARTIAL", res.note)
        self.assertEqual(rpc.submitted, [])
        self.assertEqual(ex._fee_drops, 0)

    def test_simulated_fill_below_the_guard_sends_nothing(self):
        from xrpl_arb.plan import Attempt

        ex, rpc = self.make(FakeRpc("", sim_delivered={"currency": "USD", "issuer": USD.issuer, "value": "130"}))
        res = ex._attempt(self.ev, Attempt(0, 100.0, 139.0, "leg"))
        self.assertFalse(res.ok)
        self.assertEqual(rpc.submitted, [])

    def test_validated_failure_still_pays_the_fee(self):
        from xrpl_arb.plan import Attempt

        ex, rpc = self.make(FakeRpc("", sim_delivered={"currency": "USD", "issuer": USD.issuer, "value": "140"}, tx_result="tecPATH_PARTIAL"))
        res = ex._attempt(self.ev, Attempt(0, 100.0, 139.0, "leg"))
        self.assertEqual((res.ok, res.sent), (False, True))
        self.assertEqual(ex._fee_drops, 12)

    def test_unexpected_error_mid_cycle_halts_instead_of_continuing(self):
        ex, rpc = self.make()
        ex._attempt = lambda ev, a: (_ for _ in ()).throw(RuntimeError("boom"))
        ex._busy = True
        tid = ex._begin(self.ev, 5)
        ex._run(tid, self.ev, snap_with(5))
        self.assertIsNotNone(self.state.halted)
        self.assertFalse(ex.busy)
        self.assertEqual(self.state.stats["stuck"], 1)

    def test_executor_is_never_left_busy_if_bookkeeping_fails(self):
        ex, _ = self.make()
        ex._busy = True
        ex._attempt = lambda ev, a: Result(False, note="x")
        ex._end = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bookkeeping broke"))
        with self.assertRaises(RuntimeError):
            ex._run(1, self.ev, snap_with(5))
        self.assertFalse(ex.busy)

    def test_session_loss_limit_halts(self):
        from xrpl_arb.plan import Outcome

        ex, _ = self.make(max_session_loss=1.0)
        self.state.stats["pnl_real"] = -1.5
        ex._after_trade(Outcome("LOST", -1.5, ""), -1.5)
        self.assertIn("session loss limit", self.state.halted)

    def test_three_losses_in_a_row_halt(self):
        from xrpl_arb.plan import Outcome

        ex, _ = self.make()
        for _ in range(2):
            ex._after_trade(Outcome("LOST", -0.1, ""), -0.1)
        self.assertIsNone(self.state.halted)
        ex._after_trade(Outcome("LOST", -0.1, ""), -0.1)
        self.assertIn("three losing trades", self.state.halted)

    def test_a_win_resets_the_losing_streak(self):
        from xrpl_arb.plan import Outcome

        ex, _ = self.make()
        ex._after_trade(Outcome("LOST", -0.1, ""), -0.1)
        ex._after_trade(Outcome("WON", 0.1, ""), 0.1)
        self.assertEqual(ex._losses_in_a_row, 0)


class Cli(unittest.TestCase):
    def run_cli(self, *argv, env=None):
        import contextlib
        import io
        import os
        from unittest import mock

        from xrpl_arb.__main__ import main

        with mock.patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            main(list(argv))
        return str(cm.exception)

    def test_live_trading_needs_explicit_inputs(self):
        import os

        os.environ.pop("XRPL_SEED", None)
        self.assertIn("XRPL_SEED", self.run_cli("--trade-live", "--max-trade", "5", "--no-browser"))
        self.assertIn("--max-trade", self.run_cli("--trade-live", "--no-browser", env={"XRPL_SEED": "x"}))

    def test_demo_cannot_trade_live(self):
        self.assertIn("demo", self.run_cli("--demo", "--trade-live", "--max-trade", "5"))

    def test_bad_settings_are_rejected(self):
        self.assertIn("bad settings", self.run_cli("--base", "NOPE"))
        self.assertIn("bad settings", self.run_cli("--size-min", "50", "--size-max", "10"))
        self.assertIn("--markets", self.run_cli("--markets", "XRPRLUSD"))


if __name__ == "__main__":
    unittest.main()
