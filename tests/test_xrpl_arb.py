import contextlib
import io
import json
import os
import random
import tempfile
import types
import unittest
from decimal import Decimal as D
from unittest import mock

from xrpl_arb.__main__ import main
from xrpl_arb.client import XrplClient, XrplError
from xrpl_arb.cycles import best_size, closest, find_cycles, run_cycle, scan
from xrpl_arb.liquidity import Market, Offer, Pool, _amm_in_to_price
from xrpl_arb.model import XRP, Asset, amount_value, floor_drop, is_valid_address
from xrpl_arb.snapshot import RateCache, parse_offers, parse_pool, take_snapshot
from xrpl_arb.stream import Lifetimes, PairIndex, watch

BITSTAMP = "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"
GATEHUB = "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"
USD = Asset("USD", BITSTAMP)
EUR = Asset("EUR", GATEHUB)

SCAN = dict(max_xrp=D(1000), tx_cost=D("0.00002"), min_profit=D("0.01"), min_profit_pct=D("0.1"))


def mk(a, b, offers=(), pool=None, rate=1):
    return Market(a, b, tuple(Offer(D(p), D(g)) for p, g in offers), pool, D(rate))


def triangle(usd_per_xrp="0.5", eur_per_usd="0.9", xrp_per_eur="2.5", depth=100):
    """XRP -> USD -> EUR -> XRP through three one-offer books. Product of rates > 1 means an arbitrage."""
    d = D(depth)
    return {
        (XRP, USD): mk(XRP, USD, [(d, d * D(usd_per_xrp))]),
        (USD, EUR): mk(USD, EUR, [(d * D(usd_per_xrp), d * D(usd_per_xrp) * D(eur_per_usd))]),
        (EUR, XRP): mk(EUR, XRP, [(d * D(usd_per_xrp) * D(eur_per_usd), d * D(usd_per_xrp) * D(eur_per_usd) * D(xrp_per_eur))]),
    }


class AddressesAndAssets(unittest.TestCase):
    def test_address_checksum(self):
        self.assertTrue(is_valid_address(BITSTAMP))
        self.assertTrue(is_valid_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"))
        self.assertTrue(is_valid_address("rrrrrrrrrrrrrrrrrrrrrhoLvTp"))  # leading zero bytes
        self.assertFalse(is_valid_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTX"))  # one character off
        self.assertFalse(is_valid_address(""))
        self.assertFalse(is_valid_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyT0"))  # '0' is not in the alphabet

    def test_asset_parse_and_spec(self):
        a = Asset.parse(f"USD+{BITSTAMP}")
        self.assertEqual(a, USD)
        self.assertEqual(a.spec(), {"currency": "USD", "issuer": BITSTAMP})
        self.assertEqual(Asset.parse("XRP"), XRP)
        self.assertEqual(XRP.spec(), {"currency": "XRP"})
        self.assertEqual(Asset.parse(str(a)), a)

    def test_asset_rejects_bad_input(self):
        for bad in ("USD", "USD+rNotAnAddress", f"XRP+{BITSTAMP}", f"+{BITSTAMP}", f"{'X' * 21}+{BITSTAMP}",
                    f"cafés+{BITSTAMP}", f"{'g' * 40}+{BITSTAMP}"):
            with self.assertRaises(ValueError, msg=bad):
                Asset.parse(bad)

    def test_long_names_are_encoded_the_way_the_ledger_stores_them(self):
        drop = Asset.parse("DROP+rszenFJoDdiGjyezQc8pME9KWDQH43Tswh")
        self.assertEqual(drop.currency, "44524F5000000000000000000000000000000000")
        self.assertEqual(drop.label, "DROP")
        lower = Asset.parse("44524f5000000000000000000000000000000000+rszenFJoDdiGjyezQc8pME9KWDQH43Tswh")
        self.assertEqual(lower, drop)  # a ledger response's upper-case hex finds the same asset
        self.assertEqual(Asset.parse("$TRUMP+" + BITSTAMP).label, "$TRUMP")
        self.assertEqual(Asset.parse("USDT+" + BITSTAMP).label, "USDT")
        for short in ("X", "US"):  # one- and two-character names are not standard codes either
            self.assertEqual(Asset.parse(f"{short}+{BITSTAMP}").label, short)
            self.assertEqual(len(Asset.parse(f"{short}+{BITSTAMP}").currency), 40)

    def test_hex_currency_label(self):
        solo = Asset("534F4C4F00000000000000000000000000000000", "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz")
        self.assertEqual(solo.label, "SOLO")
        self.assertTrue(solo.short().startswith("SOLO+"))

    def test_amounts(self):
        self.assertEqual(amount_value("2500000"), D("2.5"))  # XRP arrives as drops
        self.assertEqual(amount_value({"currency": "USD", "issuer": BITSTAMP, "value": "1.5e1"}), D(15))
        self.assertEqual(floor_drop(D("1.9999999")), D("1.999999"))


class PoolMaths(unittest.TestCase):
    def test_swap_matches_constant_product_with_fee(self):
        pool = Pool(XRP, USD, D(1000), D(500), D("0.003"))
        dy, after = pool.swap(XRP, D(10))
        m = D("0.997")
        self.assertEqual(dy, D(500) * m * 10 / (1000 + m * 10))
        self.assertEqual((after.ra, after.rb), (D(1010), D(500) - dy))  # the whole input joins the reserve

    def test_orientation_does_not_matter(self):
        pool = Pool(XRP, USD, D(1000), D(500), D("0.003"))
        flipped = Pool(USD, XRP, D(500), D(1000), D("0.003"))
        self.assertEqual(pool.swap(USD, D(5))[0], flipped.swap(USD, D(5))[0])
        _, after = pool.swap(USD, D(5))
        self.assertEqual((after.a, after.ra), (XRP, D(1000) - pool.swap(USD, D(5))[0]))

    def test_fee_free_pool_keeps_the_product_and_a_fee_grows_it(self):
        pool = Pool(XRP, USD, D(1000), D(500), D(0))
        _, after = pool.swap(XRP, D(100))
        self.assertLess(abs(after.ra * after.rb / (pool.ra * pool.rb) - 1), D("1e-20"))
        pool = Pool(XRP, USD, D(1000), D(500), D("0.01"))
        _, after = pool.swap(XRP, D(100))
        self.assertGreater(after.ra * after.rb / (pool.ra * pool.rb), D("1.0005"))

    def test_input_sized_to_a_price_lands_the_pool_exactly_on_it(self):
        # the fee stays in the reserve, so this is the quadratic, not the fee-free square root
        for fee in (D(0), D("0.003"), D("0.01")):
            pool = Pool(XRP, USD, D(1000), D(500), fee)
            m = 1 - fee
            for price in (D("2.1"), D("2.5"), D(4)):
                dx = _amm_in_to_price(D(1000), D(500), m, price)
                self.assertGreater(dx, 0)
                _, after = pool.swap(XRP, dx)
                spot = after.ra / (m * after.rb)  # in per out
                self.assertLess(abs(spot / price - 1), D("1e-20"))
            self.assertEqual(_amm_in_to_price(D(1000), D(500), m, D(2)), 0)  # already at or above 2 in per out


class MarketQuotes(unittest.TestCase):
    def test_book_walk_with_partial_fill(self):
        m = mk(XRP, USD, [(10, 20), (10, 15)])
        self.assertEqual(m.quote(D(15))[0], D("27.5"))  # all of the first offer, half of the second
        self.assertEqual(m.quote(D(100))[0], D(35))  # beyond the book nothing more arrives
        self.assertEqual(m.quote(D(0))[0], D(0))

    def test_pool_only_matches_pool_formula(self):
        pool = Pool(XRP, USD, D(1000), D(500), D("0.003"))
        m = mk(XRP, USD, pool=pool)
        self.assertEqual(m.quote(D(10))[0], pool.swap(XRP, D(10))[0])

    def test_transfer_rate_is_charged_on_what_we_spend(self):
        plain = mk(USD, XRP, [(10, 20)])
        taxed = mk(USD, XRP, [(10, 20)], rate="1.01")
        self.assertEqual(taxed.quote(D("10.1"))[0], plain.quote(D(10))[0])
        self.assertLess(taxed.quote(D(10))[0], plain.quote(D(10))[0])
        self.assertAlmostEqual(float(taxed.spot_rate()), float(plain.spot_rate()) / 1.01)

    def test_small_trade_uses_only_the_cheaper_pool(self):
        pool = Pool(XRP, USD, D(1000), D(500), D(0))  # 0.5 USD per XRP, i.e. 2 XRP-in per USD-out
        m = mk(XRP, USD, [(10, 4)], pool)  # book sells USD at 2.5 XRP each: worse than the pool
        self.assertEqual(m.quote(D(1))[0], pool.swap(XRP, D(1))[0])

    def test_spot_rate_is_the_limit_of_small_trades(self):
        pool = Pool(XRP, USD, D(1000), D(500), D("0.003"))
        for m in (mk(XRP, USD, pool=pool), mk(XRP, USD, [(10, 6)], pool), mk(XRP, USD, [(10, 4)], pool, rate="1.005")):
            eps = D("0.000001")
            self.assertAlmostEqual(float(m.quote(eps)[0] / eps), float(m.spot_rate()), places=3)

    def test_split_is_never_worse_than_any_other_split(self):
        # With no fee the routing is exactly optimal. With a fee the AMM is taken in pieces between book offers,
        # as the ledger does, and every piece pays the fee again, so it can land a hair under one single-shot split.
        rng = random.Random(7)
        for _ in range(300):
            x, y = D(rng.uniform(100, 5000)), D(rng.uniform(100, 5000))
            fee = D(rng.choice([0, 0.001, 0.005]))
            pool = Pool(XRP, USD, x, y, fee)
            spot = y / x
            offers = tuple(Offer(D(rng.uniform(1, 40)), D(rng.uniform(1, 40)) * (1 + D(rng.uniform(-0.1, 0.1))) * spot / 1)
                           for _ in range(rng.randint(1, 5)))
            offers = tuple(sorted(offers, key=lambda o: o.price))
            both = Market(XRP, USD, offers, pool)
            book = Market(XRP, USD, offers)
            amm = Market(XRP, USD, (), pool)
            for size in (D(1), D(15), D(60), D(400)):
                best = both.quote(size)[0]
                for s in (D(0), D("0.25"), D("0.5"), D("0.75"), D(1)):
                    alt = book.quote(size * s)[0] + amm.quote(size * (1 - s))[0]
                    slack = D("1e-18") if fee == 0 else alt * D("5e-4")
                    self.assertGreaterEqual(best + slack, alt, msg=f"fee {fee} size {size} split {s}")


class Cycles(unittest.TestCase):
    def test_find_cycles(self):
        markets = {}
        for a in (XRP, USD, EUR):
            for b in (XRP, USD, EUR):
                if a != b:
                    markets[(a, b)] = mk(a, b, [(1, 1)])
        self.assertEqual(len(find_cycles(markets, XRP, 2)), 2)
        three = find_cycles(markets, XRP, 3)
        self.assertEqual(len(three), 4)
        self.assertEqual({len(c) for c in three}, {2, 3})
        del markets[(XRP, USD)], markets[(EUR, USD)]
        self.assertEqual(len(find_cycles(markets, XRP, 3)), 1)  # only XRP -> EUR -> XRP is left that starts and ends

    def test_empty_markets_are_ignored(self):
        markets = triangle()
        markets[(XRP, EUR)] = Market(XRP, EUR)
        self.assertEqual(len(find_cycles(markets, XRP, 3)), 1)

    def test_triangle_arbitrage_is_found_at_full_depth(self):
        cycle = tuple(triangle().values())
        self.assertEqual(run_cycle(cycle, D(100)), D("112.5"))
        found = scan(triangle(), **SCAN)
        self.assertEqual(len(found), 1)
        opp = found[0]
        self.assertEqual(opp.path, ["XRP", str(USD), str(EUR), "XRP"])
        self.assertAlmostEqual(float(opp.amount_in), 100, places=4)
        self.assertAlmostEqual(float(opp.profit), 12.5 - 0.00002, places=4)
        self.assertEqual([l.amount_in for l in opp.legs][1:], [l.amount_out for l in opp.legs][:-1])  # legs chain

    def test_closest_reports_distance_to_profit(self):
        near = closest(triangle(xrp_per_eur="2.0"))  # 0.5 * 0.9 * 2.0 = 0.9
        self.assertAlmostEqual(float(near[0]), -10.0, places=6)
        self.assertEqual([m.asset_in for m in near[1]], [XRP, USD, EUR])
        self.assertAlmostEqual(float(closest(triangle())[0]), 12.5, places=6)  # 1.125 is a profit
        self.assertIsNone(closest({}))
        self.assertIsNone(closest({(XRP, USD): mk(XRP, USD, [(1, 1)])}))  # no way back to XRP

    def test_no_arbitrage_when_product_of_rates_is_below_one(self):
        self.assertEqual(scan(triangle(xrp_per_eur="2.0"), **SCAN), [])  # 0.5 * 0.9 * 2.0 = 0.9

    def test_transfer_fee_can_erase_the_edge(self):
        markets = triangle(xrp_per_eur="2.3")  # 1.035 gross
        self.assertEqual(len(scan(markets, **SCAN)), 1)
        markets[(USD, EUR)] = mk(USD, EUR, [(50, 45)], rate="1.03")
        markets[(EUR, XRP)] = mk(EUR, XRP, [(45, 103.5)], rate="1.03")
        self.assertEqual(scan(markets, **SCAN), [])

    def test_thresholds_apply_after_the_network_fee(self):
        markets = triangle(xrp_per_eur="2.3")  # about 3.5% on 100 XRP
        self.assertEqual(len(scan(markets, **{**SCAN, "min_profit": D(3)})), 1)
        self.assertEqual(scan(markets, **{**SCAN, "min_profit": D(4)}), [])
        self.assertEqual(scan(markets, **{**SCAN, "min_profit_pct": D(5)}), [])
        self.assertEqual(scan(markets, **{**SCAN, "tx_cost": D(5)}), [])

    def test_max_xrp_caps_the_size(self):
        found = scan(triangle(), **{**SCAN, "max_xrp": D(10)})
        self.assertAlmostEqual(float(found[0].amount_in), 10, places=4)
        self.assertAlmostEqual(float(found[0].profit), 1.25 - 0.00002, places=4)

    def test_best_size_matches_a_brute_force_search_on_smooth_curves(self):
        pools = {
            (XRP, USD): Pool(XRP, USD, D(5000), D(2500), D("0.003")),
            (USD, EUR): Pool(USD, EUR, D(3000), D(2900), D("0.003")),
            (EUR, XRP): Pool(EUR, XRP, D(2000), D(4600), D("0.003")),  # EUR is dear against XRP elsewhere
        }
        cycle = tuple(Market(a, b, (), p) for (a, b), p in pools.items())
        x = best_size(cycle, D(1000))
        self.assertGreater(x, 0)
        grid = max(run_cycle(cycle, D(g)) - D(g) for g in range(1, 1000))
        self.assertGreaterEqual(run_cycle(cycle, x) - x + D("0.01"), grid)
        self.assertLess(x, 1000)  # a real interior optimum, not the cap

    def test_unprofitable_cycle_sizes_to_zero(self):
        cycle = tuple(triangle(xrp_per_eur="2.0").values())
        self.assertEqual(best_size(cycle, D(100)), 0)

    def test_amm_only_round_trip_never_looks_profitable(self):
        pool = Pool(XRP, USD, D(1000), D(500), D("0.003"))
        markets = {(XRP, USD): Market(XRP, USD, (), pool), (USD, XRP): Market(USD, XRP, (), pool)}
        self.assertEqual(scan(markets, **SCAN), [])

    def test_a_pool_touched_twice_carries_the_first_trade_forward(self):
        pool = Pool(XRP, USD, D(1000), D(500), D("0.003"))
        leg1 = Market(XRP, USD, (), pool)
        leg2 = Market(USD, XRP, (), pool)
        usd, after = leg1.quote(D(100))
        expected, _ = leg2.quote(usd, after)
        self.assertEqual(run_cycle((leg1, leg2), D(100)), floor_drop(expected))
        self.assertNotEqual(expected, leg2.quote(usd)[0])  # ignoring the first trade would give another number

    def test_book_against_pool_arbitrage(self):
        pool = Pool(XRP, USD, D(10000), D(5000), D("0.003"))  # 0.5 USD per XRP
        markets = {
            (XRP, USD): Market(XRP, USD, (), pool),
            (USD, XRP): Market(USD, XRP, (Offer(D(400), D(1000)),), pool),  # book sells XRP at 0.4 USD
        }
        found = scan(markets, **SCAN)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, ["XRP", str(USD), "XRP"])
        self.assertGreater(found[0].profit, 0)


def iou(asset, value):
    return {"currency": asset.currency, "issuer": asset.issuer, "value": str(value)}


def amt(asset, value):
    return str(int(D(value) * 1_000_000)) if asset.is_xrp else iou(asset, value)


class FakeLedger:
    """Answers like a rippled server, from a table: books[(asset_in, asset_out)] = [(pays, gets), ...]."""

    def __init__(self, books=None, pools=None, rates=None, ledger=100, missing_ledgers=()):
        self.books, self.pools, self.rates, self.ledger = books or {}, pools or {}, rates or {}, ledger
        self.missing_ledgers = set(missing_ledgers)  # pinned indexes this "node" does not have
        self.calls = []

    def _check(self, ledger):
        if ledger in self.missing_ledgers:
            raise XrplError("lgrNotFound")
        return self.ledger if ledger == "validated" else ledger

    def validated_ledger(self):
        return self.ledger

    def book_offers(self, taker_gets, taker_pays, limit=60, ledger="validated"):
        self.calls.append(("book", taker_gets, taker_pays, ledger))
        at = self._check(ledger)
        offers = [{"TakerGets": amt(taker_gets, g), "TakerPays": amt(taker_pays, p), "quality": "1"}
                  for p, g in self.books.get((taker_pays, taker_gets), [])]
        return {"offers": offers, "ledger_index": at}

    def amm_info(self, a, b, ledger="validated"):
        self.calls.append(("amm", a, b, ledger))
        at = self._check(ledger)
        pool = self.pools.get(frozenset((a, b)))
        if pool is None:
            raise XrplError("actNotFound")
        (a1, v1), (a2, v2), fee = pool
        return {"amm": {"amount": amt(a1, v1), "amount2": amt(a2, v2), "trading_fee": fee}, "ledger_index": at}

    def transfer_rate(self, issuer):
        return D(self.rates.get(issuer, 1))


def triangle_ledger(**kw):
    return FakeLedger(books={(XRP, USD): [(100, 50)], (USD, EUR): [(50, 45)], (EUR, XRP): [(45, "112.5")]}, **kw)


class SnapshotParsing(unittest.TestCase):
    def test_funded_amounts_win_over_the_offer_size(self):
        res = {"offers": [
            {"TakerGets": iou(USD, 100), "TakerPays": "200000000", "taker_gets_funded": iou(USD, 40), "taker_pays_funded": "80000000"},
            {"TakerGets": iou(USD, 10), "TakerPays": "15000000"},
            {"TakerGets": iou(USD, 100), "TakerPays": "400000000", "taker_gets_funded": iou(USD, 50)},  # pays scales with it
        ]}
        offers = parse_offers(res)
        self.assertEqual([(o.pays, o.gets) for o in offers], [(D(15), D(10)), (D(80), D(40)), (D(200), D(50))])
        self.assertEqual([o.price for o in offers], sorted(o.price for o in offers))

    def test_unfunded_and_empty_offers_are_dropped(self):
        res = {"offers": [{"TakerGets": iou(USD, 100), "TakerPays": "1000000", "taker_gets_funded": iou(USD, 0)},
                          {"TakerGets": iou(USD, 0), "TakerPays": "1000000"}]}
        self.assertEqual(parse_offers(res), ())

    def test_pool(self):
        res = {"amm": {"amount": "1000000000", "amount2": iou(USD, 500), "trading_fee": 300}}
        pool = parse_pool(res, USD, XRP)
        self.assertEqual((pool.a, pool.ra, pool.b, pool.rb, pool.fee), (XRP, D(1000), USD, D(500), D("0.003")))
        with self.assertRaises(XrplError):
            parse_pool(res, EUR, XRP)
        res["amm"]["amount"] = "0"
        self.assertIsNone(parse_pool(res, USD, XRP))

    def test_auction_slot_members_pay_the_discounted_fee_while_the_slot_is_live(self):
        holder, member, outsider = BITSTAMP, GATEHUB, "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
        slot = {"account": holder, "auth_accounts": [{"account": member}], "discounted_fee": 39,
                "expiration": "2026-10-21T22:56:11+0000", "time_interval": 4}  # shape of a live mainnet slot
        res = {"amm": {"amount": "1000000000", "amount2": iou(USD, 500), "trading_fee": 393, "auction_slot": slot}}
        fee = lambda who: parse_pool(res, USD, XRP, who).fee
        self.assertEqual((fee(holder), fee(member)), (D("0.00039"), D("0.00039")))
        self.assertEqual((fee(outsider), fee(None)), (D("0.00393"), D("0.00393")))  # default is the full fee
        slot["time_interval"] = 20  # an expired slot, as on mainnet: time_interval 20 and a past expiration
        self.assertEqual(fee(holder), D("0.00393"))
        del res["amm"]["auction_slot"]  # a pool that never had an auction
        self.assertEqual(fee(holder), D("0.00393"))
        res["amm"]["auction_slot"] = {"account": holder, "auth_accounts": None, "discounted_fee": 39, "time_interval": 4}
        self.assertEqual(fee(holder), D("0.00039"))  # mainnet really does send a null list for some pools

    def test_an_unreadable_response_skips_that_pair_instead_of_stopping_the_scan(self):
        ledger = FakeLedger(books={(XRP, USD): [(10, 5)]}, pools={frozenset((XRP, USD)): ((XRP, 100), (USD, 50), 300)})
        real_amm = ledger.amm_info

        def amm_info(a, b, ledger="validated"):
            res = real_amm(a, b, ledger)
            if EUR in (a, b):
                res["amm"].pop("trading_fee")  # malformed
            return res

        ledger.amm_info = amm_info
        ledger.pools[frozenset((XRP, EUR))] = ((XRP, 100), (EUR, 50), 300)
        snap = take_snapshot(ledger, [USD, EUR], RateCache(ledger))
        self.assertEqual(len(snap.skipped), 1)
        self.assertIn("pool", snap.skipped[0])
        self.assertIsNone(snap.markets[(XRP, EUR)].pool)
        self.assertIsNotNone(snap.markets[(XRP, USD)].pool)

    def test_take_snapshot_builds_every_direction_with_rates_and_pools(self):
        pool = ((XRP, 1000), (USD, 500), 300)
        ledger = FakeLedger(books={(XRP, USD): [(10, 5)]}, pools={frozenset((XRP, USD)): pool},
                            rates={BITSTAMP: "1.005"}, ledger=77)
        snap = take_snapshot(ledger, [USD, EUR], RateCache(ledger))
        self.assertEqual(len(snap.markets), 6)  # 3 nodes, every ordered pair
        self.assertEqual((snap.ledger, snap.consistent, snap.books, snap.pools), (77, True, 1, 1))
        fwd, back = snap.markets[(XRP, USD)], snap.markets[(USD, XRP)]
        self.assertEqual(len(fwd.offers), 1)
        self.assertEqual(back.offers, ())
        self.assertIs(fwd.pool, back.pool)
        self.assertEqual((fwd.in_rate, back.in_rate), (D(1), D("1.005")))
        self.assertTrue(snap.markets[(EUR, USD)].empty)

    def test_every_read_is_pinned_to_one_validated_ledger(self):
        ledger = FakeLedger(ledger=555)
        snap = take_snapshot(ledger, [USD, EUR], RateCache(ledger))
        self.assertEqual(len(ledger.calls), 6 + 3)  # 6 ordered books + 3 pools
        self.assertEqual({c[-1] for c in ledger.calls}, {555})
        self.assertEqual((snap.ledger, snap.consistent), (555, True))

    def test_a_node_without_the_pinned_ledger_falls_back_and_says_so(self):
        ledger = FakeLedger(books={(XRP, USD): [(10, 5)]}, ledger=555, missing_ledgers=[555])
        snap = take_snapshot(ledger, [USD], RateCache(ledger))
        self.assertFalse(snap.consistent)
        self.assertEqual(len(snap.markets[(XRP, USD)].offers), 1)  # still got the data from "validated"
        self.assertIn("validated", {c[-1] for c in ledger.calls})

    def test_other_ledger_errors_are_not_swallowed(self):
        ledger = FakeLedger()
        ledger.book_offers = mock.Mock(side_effect=XrplError("invalidParams"))
        with self.assertRaises(XrplError):
            take_snapshot(ledger, [USD], RateCache(ledger))

    def test_pairs_limit_what_is_read(self):
        ledger = FakeLedger(books={(XRP, USD): [(10, 5)], (USD, EUR): [(5, 4)]})
        snap = take_snapshot(ledger, [], RateCache(ledger), pairs=[(XRP, USD), (EUR, USD), (USD, EUR)])
        self.assertEqual(set(snap.markets), {(XRP, USD), (USD, XRP), (EUR, USD), (USD, EUR)})
        self.assertEqual(len(ledger.calls), 4 + 2)  # (EUR, USD) and (USD, EUR) are one pair: 2 pools, 4 books
        self.assertEqual(len(snap.markets[(USD, EUR)].offers), 1)

    def test_parallel_and_serial_reads_agree(self):
        books = {(XRP, USD): [(10, 5), (20, 9)], (USD, EUR): [(5, 4)], (EUR, XRP): [(4, 12)]}
        pools = {frozenset((XRP, EUR)): ((XRP, 100), (EUR, 50), 300)}
        results = []
        for workers in (1, 8):
            ledger = FakeLedger(books=books, pools=pools)
            snap = take_snapshot(ledger, [USD, EUR], RateCache(ledger), workers=workers)
            results.append({k: (m.offers, m.pool) for k, m in snap.markets.items()})
        self.assertEqual(results[0], results[1])

    def test_rate_cache_asks_each_issuer_once(self):
        ledger = FakeLedger(rates={BITSTAMP: "1.002"})
        with mock.patch.object(ledger, "transfer_rate", wraps=ledger.transfer_rate) as spy:
            rates = RateCache(ledger)
            self.assertEqual([rates.get(USD), rates.get(USD), rates.get(XRP)], [D("1.002"), D("1.002"), D(1)])
            spy.assert_called_once_with(BITSTAMP)


class ClientBehaviour(unittest.TestCase):
    def setUp(self):
        p = mock.patch("xrpl_arb.client.time.sleep")
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def reply(result):
        return json.dumps({"result": result}).encode()

    def test_request_shape_and_result(self):
        seen = []

        def post(url, body, timeout):
            seen.append((url, json.loads(body)))
            return self.reply({"status": "success", "x": 1})

        c = XrplClient(["https://a/"], post=post)
        self.assertEqual(c.call("server_info", foo="bar")["x"], 1)
        self.assertEqual(seen, [("https://a/", {"method": "server_info", "params": [{"foo": "bar"}]})])

    def test_ledger_errors_raise_immediately(self):
        calls = []

        def post(url, body, timeout):
            calls.append(url)
            return self.reply({"status": "error", "error": "actNotFound", "error_message": "Account not found."})

        with self.assertRaises(XrplError) as cm:
            XrplClient(["https://a/", "https://b/"], post=post).call("amm_info")
        self.assertEqual(cm.exception.code, "actNotFound")
        self.assertEqual(len(calls), 1)

    def test_network_errors_and_busy_servers_move_to_the_next_url(self):
        replies = [OSError("down"), self.reply({"status": "error", "error": "slowDown"}), b"not json",
                   self.reply({"status": "success", "ok": True})]
        urls = []

        def post(url, body, timeout):
            urls.append(url)
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        c = XrplClient(["https://a/", "https://b/"], retries=3, post=post)
        self.assertTrue(c.call("fee")["ok"])
        self.assertEqual(urls, ["https://a/", "https://b/", "https://a/", "https://b/"])

    def test_the_server_that_answered_is_tried_first_next_time(self):
        urls = []

        def post(url, body, timeout):
            urls.append(url)
            if url == "https://a/":
                raise OSError("blocked")
            return self.reply({"status": "success", "ledger_index": 9})

        c = XrplClient(["https://a/", "https://b/"], post=post)
        self.assertEqual(c.validated_ledger(), 9)
        self.assertEqual(c.validated_ledger(), 9)
        self.assertEqual(urls, ["https://a/", "https://b/", "https://b/"])  # no second visit to the dead server

    def test_gives_up_after_the_retries(self):
        def post(url, body, timeout):
            raise OSError("down")

        with self.assertRaises(XrplError) as cm:
            XrplClient(["https://a/"], retries=2, post=post).call("fee")
        self.assertEqual(cm.exception.code, "unreachable")

    def test_transfer_rate(self):
        def make(data):
            return XrplClient(["https://a/"], post=lambda u, b, t: self.reply({"status": "success", "account_data": data}))

        self.assertEqual(make({"TransferRate": 1005000000}).transfer_rate(BITSTAMP), D("1.005"))
        self.assertEqual(make({}).transfer_rate(BITSTAMP), D(1))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "markets.json")
        with open(self.cfg, "w") as f:
            json.dump({"assets": [str(USD), str(EUR)]}, f)

    @staticmethod
    def rows(path):
        with open(path) as f:
            return [json.loads(line) for line in f]

    def run_main(self, argv, client):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv, client=client)
        return code, out.getvalue()

    def test_scan_prints_and_records_an_opportunity(self):
        log = os.path.join(self.tmp.name, "sub", "opps.jsonl")
        code, text = self.run_main(["scan", "--config", self.cfg, "--out", log, "--max-xrp", "1000",
                                    "--tx-cost", "0.00002"], triangle_ledger())
        self.assertEqual(code, 0)
        self.assertIn("1 opportunities", text)
        self.assertIn("+12.4999", text)  # 12.5 less the fee, within a drop of the optimum size
        rows = self.rows(log)
        self.assertEqual([r["type"] for r in rows], ["scan", "opp"])
        self.assertEqual((rows[0]["opportunities"], rows[0]["ledger"], rows[0]["consistent"]), (1, 100, True))
        self.assertEqual(rows[1]["ledger"], 100)
        self.assertEqual(rows[1]["path"][0], "XRP")
        self.assertAlmostEqual(float(D(rows[1]["profit"])), 12.5 - 0.00002, places=4)

    def test_a_quiet_market_records_only_the_scan_row(self):
        log = os.path.join(self.tmp.name, "opps.jsonl")
        ledger = FakeLedger(books={(XRP, USD): [(100, 50)], (USD, XRP): [(50, 90)]})  # round trip returns 90%
        code, text = self.run_main(["scan", "--config", self.cfg, "--out", log], ledger)
        self.assertEqual(code, 0)
        self.assertIn("0 opportunities", text)
        self.assertIn("nothing pays at any size; nearest cycle returns -10.000%", text)  # "none" vs "almost"
        rows = self.rows(log)
        self.assertEqual([r["type"] for r in rows], ["scan"])
        self.assertEqual((rows[0]["candidates"], rows[0]["best_below_thresholds"]), (0, None))

    def test_a_cycle_that_misses_the_thresholds_is_reported_as_the_near_miss(self):
        log = os.path.join(self.tmp.name, "opps.jsonl")
        code, text = self.run_main(["scan", "--config", self.cfg, "--out", log, "--min-profit", "50"], triangle_ledger())
        self.assertEqual(code, 0)
        self.assertIn("0 opportunities", text)
        self.assertIn("best below your thresholds: +12.49", text)
        rows = self.rows(log)
        self.assertEqual([r["type"] for r in rows], ["scan"])  # not recorded as an opportunity
        self.assertEqual(rows[0]["candidates"], 1)
        self.assertAlmostEqual(float(D(rows[0]["best_below_thresholds"]["profit"])), 12.5, places=3)

    def test_config_with_pairs_reads_only_those_pairs(self):
        with open(self.cfg, "w") as f:
            json.dump({"assets": [str(EUR)], "pairs": [["XRP", str(USD)], [str(USD), str(EUR)]]}, f)
        ledger = triangle_ledger()
        code, text = self.run_main(["scan", "--config", self.cfg], ledger)
        self.assertEqual(code, 0)
        self.assertEqual({c[0] for c in ledger.calls}, {"book", "amm"})
        self.assertEqual(len(ledger.calls), 4 + 2)  # two pairs: four books and two pools; "assets" is ignored
        self.assertIn("0 opportunities", text)  # no EUR/XRP market, so no triangle to close

    def test_bad_config_stops_before_any_request(self):
        for body in ({"assets": ["USD+rNotAnAddress"]}, {"assets": []}, {}, {"pairs": []}, {"pairs": [["XRP"]]},
                     {"pairs": [["XRP", "XRP"]]}, {"pairs": [["XRP", "USD+rNotAnAddress"]]}):
            with open(self.cfg, "w") as f:
                json.dump(body, f)
            ledger = FakeLedger()
            with self.assertRaises(SystemExit):
                main(["scan", "--config", self.cfg], client=ledger)
            self.assertEqual(ledger.calls, [])

    def test_failed_single_scan_returns_nonzero(self):
        ledger = FakeLedger()
        ledger.amm_info = mock.Mock(side_effect=XrplError("unreachable", "no route"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(["scan", "--config", self.cfg], client=ledger)
        self.assertEqual(code, 1)
        self.assertIn("scan failed", err.getvalue())


AMM_ACCT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


def node(kind, entry, **fields):
    key = {"Created": "CreatedNode", "Modified": "ModifiedNode", "Deleted": "DeletedNode"}[kind]
    return {key: {"LedgerEntryType": entry, "NewFields" if kind == "Created" else "FinalFields": fields}}


def tx_msg(*nodes, ledger=100, validated=True):
    return json.dumps({"type": "transaction", "validated": validated, "ledger_index": ledger,
                       "meta": {"AffectedNodes": list(nodes)}})


def close_msg(n):
    return json.dumps({"type": "ledgerClosed", "ledger_index": n, "ledger_time": 1})


class StreamMapping(unittest.TestCase):
    def setUp(self):
        self.index = PairIndex([(XRP, USD), (USD, EUR), (EUR, USD)])  # the last two are one pair
        self.index.learn_accounts({frozenset((XRP, USD)): Pool(XRP, USD, D(1), D(1), D(0), AMM_ACCT)})

    def test_pairs_are_deduplicated(self):
        self.assertEqual(len(self.index.pairs), 2)

    def test_offer_nodes_name_their_two_assets_in_either_order(self):
        usd, eur = iou(USD, 5), iou(EUR, 5)
        for kind in ("Created", "Modified", "Deleted"):
            msg = json.loads(tx_msg(node(kind, "Offer", TakerGets=usd, TakerPays=eur)))
            self.assertEqual(self.index.touched(msg), {frozenset((USD, EUR))}, kind)
        msg = json.loads(tx_msg(node("Modified", "Offer", TakerGets=eur, TakerPays=usd)))
        self.assertEqual(self.index.touched(msg), {frozenset((USD, EUR))})
        msg = json.loads(tx_msg(node("Modified", "Offer", TakerGets=iou(USD, 1), TakerPays="1000000")))
        self.assertEqual(self.index.touched(msg), {frozenset((XRP, USD))})

    def test_an_offer_for_an_unconfigured_pair_is_ignored(self):
        other = Asset("GBP", BITSTAMP)
        msg = json.loads(tx_msg(node("Modified", "Offer", TakerGets=iou(other, 1), TakerPays="1000000")))
        self.assertEqual(self.index.touched(msg), set())

    def test_a_swap_shows_up_as_the_amm_accounts_balances_changing(self):
        xrp_side = json.loads(tx_msg(node("Modified", "AccountRoot", Account=AMM_ACCT, Balance="5")))
        iou_side = json.loads(tx_msg(node("Modified", "RippleState", HighLimit={"issuer": BITSTAMP, "value": "0"},
                                          LowLimit={"issuer": AMM_ACCT, "value": "0"})))
        for msg in (xrp_side, iou_side):
            self.assertEqual(self.index.touched(msg), {frozenset((XRP, USD))})

    def test_unrelated_and_empty_metadata(self):
        other_acct = json.loads(tx_msg(node("Modified", "AccountRoot", Account=BITSTAMP),
                                       node("Modified", "DirectoryNode", Owner=AMM_ACCT)))
        self.assertEqual(self.index.touched(other_acct), set())
        self.assertEqual(self.index.touched({}), set())
        self.assertEqual(self.index.touched({"meta": {}}), set())

    def test_currency_case_in_the_metadata_does_not_matter(self):
        solo = Asset("534F4C4F00000000000000000000000000000000", "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz")
        index = PairIndex([(XRP, solo)])
        lower = {"currency": solo.currency.lower(), "issuer": solo.issuer, "value": "1"}
        msg = json.loads(tx_msg(node("Modified", "Offer", TakerGets=lower, TakerPays="1000000")))
        self.assertEqual(index.touched(msg), {frozenset((XRP, solo))})


class OpportunityLifetimes(unittest.TestCase):
    def opp(self, profit):
        return scan(triangle(xrp_per_eur=str(D("2.5") + D(profit))), **SCAN)[0]

    def test_new_then_alive_then_ended(self):
        t = Lifetimes()
        a, b = self.opp("0"), self.opp("0.1")
        new, ended = t.update(10, [a])
        self.assertEqual((len(new), ended), (1, []))
        new, ended = t.update(11, [b])  # same path, better profit: not new
        self.assertEqual((new, ended), ([], []))
        new, ended = t.update(12, [])
        self.assertEqual(new, [])
        self.assertEqual((ended[0]["first_ledger"], ended[0]["last_ledger"], ended[0]["ledgers_alive"]), (10, 11, 2))
        self.assertEqual(D(ended[0]["best_profit"]), b.profit)  # remembers the best moment
        self.assertEqual(t.update(13, []), ([], []))  # and forgets it once reported


class FakeWS:
    """A scripted websocket: strings are messages, callables run when reached (to change the fake ledger)."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self, timeout=None):
        while self.script:
            item = self.script.pop(0)
            if callable(item):
                item()
                continue
            return item
        raise TimeoutError


def settings(**kw):
    base = dict(book_limit=60, workers=2, account=None, min_profit=0.001, min_profit_pct=0.25, max_xrp=1000,
                tx_cost=0.00002, max_legs=4, settle=0.0, full_every=1000, minutes=0, max_ledgers=0, ws=["wss://fake/"])
    base.update(kw)
    return types.SimpleNamespace(**base)


class Watching(unittest.TestCase):
    def setUp(self):
        self.ledger = FakeLedger(books={(XRP, USD): [(100, 50)], (USD, EUR): [(50, 45)], (EUR, XRP): [(45, 90)]})
        self.pairs = [(XRP, USD), (USD, EUR), (EUR, XRP)]
        self.lines, self.rows = [], []
        sink = types.SimpleNamespace(write=lambda text: self.rows.extend(json.loads(l) for l in text.splitlines()),
                                     flush=lambda: None)
        self.sink = sink
        self.eur_xrp = lambda payout: (lambda: self.ledger.books.__setitem__((EUR, XRP), [(45, payout)]))

    def run_watch(self, script, **kw):
        ws = FakeWS(script)
        code = watch(self.ledger, self.pairs, settings(**kw), connect=lambda url: ws, log=self.lines.append, sink=self.sink)
        return code, ws

    def ledger_rows(self):
        return [r for r in self.rows if r["type"] == "ledger"]

    def test_follows_an_opportunity_from_appearance_to_disappearance(self):
        offer_changed = tx_msg(node("Modified", "Offer", TakerGets=iou(EUR, 45), TakerPays="90000000"))
        code, ws = self.run_watch([
            close_msg(100),                                   # first ledger: everything is read
            self.eur_xrp(112.5), offer_changed, close_msg(101),  # EUR->XRP now pays 2.5: the triangle opens
            close_msg(102),                                   # nothing changed: still open, nothing re-read
            self.eur_xrp(90), offer_changed, close_msg(103),  # back to 2.0: it closes
        ], max_ledgers=4)
        self.assertEqual(code, 0)
        self.assertEqual(ws.sent[0], {"command": "subscribe", "streams": ["ledger", "transactions"]})
        rows = self.ledger_rows()
        self.assertEqual([(r["ledger"], r["full"], r["pairs_read"], r["opportunities"]) for r in rows],
                         [(100, True, 3, 0), (101, False, 1, 1), (102, False, 0, 1), (103, False, 1, 0)])
        opps = [r for r in self.rows if r["type"] == "opp"]
        self.assertEqual([r["ledger"] for r in opps], [101])  # recorded once, on first sighting
        end = next(r for r in self.rows if r["type"] == "end")
        self.assertEqual((end["first_ledger"], end["last_ledger"], end["ledgers_alive"]), (101, 102, 2))
        self.assertTrue(any("NEW" in l for l in self.lines) and any("ended after 2 ledger" in l for l in self.lines))

    def test_only_validated_transactions_count(self):
        offer_changed = tx_msg(node("Modified", "Offer", TakerGets=iou(EUR, 45), TakerPays="90000000"), validated=False)
        self.run_watch([close_msg(100), self.eur_xrp(112.5), offer_changed, close_msg(101)], max_ledgers=2)
        self.assertEqual([r["pairs_read"] for r in self.ledger_rows()], [3, 0])

    def test_everything_is_re_read_every_full_every_ledgers(self):
        self.run_watch([close_msg(100 + i) for i in range(5)], max_ledgers=5, full_every=2)
        self.assertEqual([r["full"] for r in self.ledger_rows()], [True, False, False, True, False])

    def test_reads_are_pinned_to_the_closed_ledger(self):
        self.run_watch([close_msg(555)], max_ledgers=1)
        self.assertEqual({c[-1] for c in self.ledger.calls}, {555})

    def test_a_dropped_connection_reconnects_and_reads_everything_again(self):
        conns = [OSError("down"), FakeWS([close_msg(100)]), FakeWS([close_msg(101)])]
        used = []

        def connect(url):
            c = conns.pop(0)
            if isinstance(c, Exception):
                raise c
            used.append(c)
            return c

        with mock.patch("xrpl_arb.stream.time.sleep"):
            watch(self.ledger, self.pairs, settings(max_ledgers=2), connect=connect, log=self.lines.append, sink=self.sink)
        self.assertEqual(len(used), 2)
        self.assertEqual([r["full"] for r in self.ledger_rows()], [True, True])  # fresh connection: full read each time
        self.assertTrue(any("stream error" in l for l in self.lines))

    def test_cli_watch_writes_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, log = os.path.join(tmp, "m.json"), os.path.join(tmp, "w.jsonl")
            with open(cfg, "w") as f:
                json.dump({"pairs": [["XRP", str(USD)], [str(USD), str(EUR)], [str(EUR), "XRP"]]}, f)
            ws = FakeWS([close_msg(100), close_msg(101)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["watch", "--config", cfg, "--out", log, "--settle", "0", "--max-ledgers", "2"],
                            client=self.ledger, connect=lambda url: ws)
            self.assertEqual(code, 0)
            with open(log) as f:
                rows = [json.loads(l) for l in f]
        self.assertEqual([r["ledger"] for r in rows if r["type"] == "ledger"], [100, 101])
        self.assertIn("connected to wss://xrplcluster.com/", out.getvalue())


if __name__ == "__main__":
    unittest.main()
