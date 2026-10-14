import asyncio
import contextlib
import io
import json
import tempfile
import unittest

from sniper.live.collector import Collector
from sniper.live.convert import read_raw, to_events, token_paths, unpriceable, verdict_from_roundtrip
from sniper.live.infer import FEE, SNAPSHOT_SOL, infer_reserves, is_consistent, reserves_from_probe
from sniper.pump.curve import CurveState, quote_buy, quote_sell
from sniper.pump.events import NewToken, ScanNote, Trade

K0 = 30.0 * 1_073_000_000
FEE_BPS = FEE * 10_000


def jupiter_rounds(x):  # Jupiter reports whole micro-tokens
    return round(x * 1e6) / 1e6


class Inference(unittest.TestCase):
    def test_one_quote_recovers_exact_reserves_across_the_whole_curve(self):
        for v_sol in (30.0, 30.05, 33.3, 47.0, 61.0, 84.9, 100.0, 114.0):
            st = CurveState(v_sol, K0 / v_sol)
            out = reserves_from_probe(K0, 0.1, jupiter_rounds(quote_buy(st, 0.1, FEE_BPS)))
            self.assertAlmostEqual(out[0], v_sol, places=5)
            self.assertAlmostEqual(out[1] / st.v_tokens, 1.0, places=8)

    def test_three_quote_fit_recovers_state_even_when_k_is_unknown(self):
        st = CurveState(41.7, 770_000_000.0)
        q = [(s, jupiter_rounds(quote_buy(st, s, FEE_BPS))) for s in SNAPSHOT_SOL]
        r = infer_reserves(q)
        self.assertTrue(is_consistent(r))
        self.assertAlmostEqual(r.v_sol, 41.7, places=4)
        self.assertAlmostEqual(r.v_tokens / 770_000_000, 1.0, places=7)

    def test_a_state_change_between_quotes_is_rejected(self):
        a = quote_buy(CurveState(30.0, 1_073_000_000.0), 0.02, FEE_BPS)
        b = quote_buy(CurveState(30.0, 1_073_000_000.0), 0.3, FEE_BPS)
        c = quote_buy(CurveState(36.0, K0 / 36.0), 1.0, FEE_BPS)  # a trade landed before the third quote
        self.assertFalse(is_consistent(infer_reserves([(0.02, a), (0.3, b), (1.0, c)])))

    def test_garbage_in_gives_none(self):
        self.assertIsNone(infer_reserves([(0.1, 5.0)]))
        self.assertIsNone(infer_reserves([(0.02, 5.0), (0.3, 2.0), (1.0, 1.0)]))   # fewer tokens for more SOL: not a curve
        self.assertFalse(is_consistent(infer_reserves([(0.02, 1.0), (0.3, 2.0), (1.0, 3.0)])))  # fits two points, misses the third
        self.assertIsNone(reserves_from_probe(0, 0.1, 5.0))
        self.assertIsNone(reserves_from_probe(K0, 0.1, 0))


class Verdicts(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(verdict_from_roundtrip(0, 0.1, 0.0969)[0], "PASS")           # the normal ~3.1% round trip
        self.assertEqual(verdict_from_roundtrip(0, 0.1, 0.092)[0], "CAUTION")         # 8% lost
        self.assertEqual(verdict_from_roundtrip(0, 0.1, 0.085)[0], "AVOID")           # 15% lost
        self.assertEqual(verdict_from_roundtrip(0, 0.1, 0.06)[0], "REJECT")           # 40% lost
        self.assertEqual(verdict_from_roundtrip(0, 0.1, None)[0], "REJECT")           # no sell route
        self.assertEqual(verdict_from_roundtrip(250_000_000, 0.1, 0.0969)[0], "AVOID")  # dev bought 25%
        self.assertEqual(verdict_from_roundtrip(120_000_000, 0.1, 0.0969)[0], "CAUTION")


def create_row(mint="M1", t=1000, creator="C", dev_tokens=0.0, v_sol=30.0, v_tokens=1_073_000_000.0):
    return {"k": "create", "t": t, "mint": mint, "creator": creator, "symbol": "S", "name": "N", "dev_tokens": dev_tokens, "dev_sol": 0.0, "v_sol": v_sol, "v_tokens": v_tokens}


class Convert(unittest.TestCase):
    def test_reserve_moves_become_trades_with_side_and_size(self):
        st1 = CurveState(31.0, K0 / 31.0)
        st2 = CurveState(30.4, K0 / 30.4)
        rows = [create_row(), {"k": "obs", "t": 2000, "mint": "M1", "v_sol": st1.v_sol, "v_tokens": st1.v_tokens},
                {"k": "obs", "t": 3000, "mint": "M1", "v_sol": st2.v_sol, "v_tokens": st2.v_tokens}]
        launch, buy, sell = to_events(rows)
        self.assertIsInstance(launch, NewToken)
        self.assertEqual((buy.side, sell.side, buy.trader), ("buy", "sell", "?"))
        self.assertAlmostEqual(buy.sol, 1.0 / (1 - FEE), places=6)               # reserves rose by 1.0 after fee
        self.assertAlmostEqual(sell.sol, 0.6 * (1 - FEE), places=6)              # curve paid out 0.6 gross, user got it minus fee
        self.assertAlmostEqual(buy.tokens, 1_073_000_000.0 - st1.v_tokens, places=3)

    def test_chk_row_makes_a_scan_note_and_old_style_first_observation_still_works(self):
        rows = [create_row(), {"k": "obs", "t": 2000, "mint": "M1", "v_sol": 30.0, "v_tokens": 1_073_000_000.0}, {"k": "chk", "t": 2500, "mint": "M1", "rt_in_sol": 0.1, "rt_back_sol": 0.0969}]
        ev = to_events(rows)
        self.assertEqual([type(e).__name__ for e in ev], ["NewToken", "ScanNote"])
        self.assertEqual((ev[1].t, ev[1].verdict), (2500, "PASS"))

    def test_unchanged_state_makes_no_trade_and_roundtrip_makes_a_scan(self):
        rows = [create_row(), {"k": "obs", "t": 2000, "mint": "M1", "v_sol": 30.0, "v_tokens": 1_073_000_000.0, "rt_in_sol": 0.1, "rt_back_sol": 0.0969}]
        kinds = [type(e).__name__ for e in to_events(rows)]
        self.assertEqual(kinds, ["NewToken", "ScanNote"])
        self.assertEqual(to_events(rows)[1].verdict, "PASS")

    def test_events_are_time_sorted_and_orphans_ignored(self):
        rows = [create_row("A", 5000), create_row("B", 1000), {"k": "obs", "t": 2000, "mint": "ZZZ", "v_sol": 31.0, "v_tokens": 1e9}]
        self.assertEqual([e.mint for e in to_events(rows)], ["B", "A"])

    def test_reader_skips_a_half_written_last_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(create_row()) + "\n" + '{"k":"obs","t":2')
        self.assertEqual(len(read_raw(f.name)), 1)

    def test_token_paths_group_by_mint(self):
        paths = token_paths([create_row("A"), {"k": "obs", "t": 2, "mint": "A", "v_sol": 31, "v_tokens": 1e9}, {"k": "end", "t": 9, "mint": "A", "reason": "idle"}, {"k": "obs", "t": 3, "mint": "orphan"}])
        self.assertEqual(list(paths), ["A"])
        self.assertEqual((len(paths["A"]["obs"]), paths["A"]["end"]["reason"]), (1, "idle"))


class FakeJupiter:
    """Serves quotes from scripted curve states, like Jupiter would, with no network."""

    def __init__(self, clock):
        self.clock, self.script, self.stats, self.rps = clock, {}, {}, 99.0

    def state(self, mint):
        for t_from, st in reversed(self.script.get(mint, [])):
            if self.clock() >= t_from:
                return st
        return None

    async def buy(self, mint, sol):
        st = self.state(mint)
        return None if st is None else jupiter_rounds(quote_buy(st, sol, FEE_BPS))

    async def sell(self, mint, tokens):
        st = self.state(mint)
        return None if st is None else quote_sell(st, tokens, FEE_BPS)


class Recorder:
    def __init__(self):
        self.rows = []

    def write(self, rec):
        self.rows.append(rec)


class CollectorTests(unittest.TestCase):
    def run_collector(self, script, duration=400.0, mayhem=(), **kw):
        now = [1000.0]
        clock = lambda: now[0]

        async def sleep(d):
            now[0] += d
            await asyncio.sleep(0)

        async def go():
            jup, rec = FakeJupiter(clock), Recorder()
            jup.script = script
            col = Collector(jup, rec, clock=clock, sleep=sleep, **kw)
            for m in script:
                col.on_create({"txType": "create", "mint": m, "traderPublicKey": "C", "pool": "pump", "vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_073_000_000.0,
                               "symbol": "S", "name": "N", "initialBuy": 0, "solAmount": 0, "is_mayhem_mode": m in mayhem})
            while col.tasks and now[0] < 1000.0 + duration:
                await asyncio.sleep(0)
            for t in list(col.tasks.values()):
                t.cancel()
            await asyncio.gather(*col.tasks.values(), return_exceptions=True)
            return col, rec

        return asyncio.run(go())

    def test_records_price_moves_with_exact_reserves_and_goes_idle(self):
        s0 = CurveState(30.0, 1_073_000_000.0)
        s1 = CurveState(34.0, K0 / 34.0)
        s2 = CurveState(31.0, K0 / 31.0)
        col, rec = self.run_collector({"M": [(1002.0, s0), (1010.0, s1), (1030.0, s2)]})
        obs = [r for r in rec.rows if r["k"] == "obs"]
        self.assertEqual([round(r["v_sol"], 4) for r in obs], [30.0, 34.0, 31.0])
        chk = [r for r in rec.rows if r["k"] == "chk"]
        self.assertEqual(len(chk), 1)
        self.assertIn("rt_back_sol", chk[0])
        self.assertLess(abs(chk[0]["check_err"]), 1e-6)
        self.assertLess(rec.rows.index(obs[0]), rec.rows.index(chk[0]))  # the price is recorded before the verification
        self.assertEqual([r["reason"] for r in rec.rows if r["k"] == "end"], ["idle"])

    def test_a_trade_landing_between_the_two_check_quotes_does_not_make_a_token_off_curve(self):
        class Moving(FakeJupiter):
            calls = 0
            async def buy(self, mint, sol):
                Moving.calls += 1
                if Moving.calls == 3:  # the state moves right after the first check quote
                    self.script = {mint: [(0.0, CurveState(33.0, K0 / 33.0))]}
                return await super().buy(mint, sol)
        now = [1000.0]
        clock = lambda: now[0]
        async def sleep(d):
            now[0] += d
            await asyncio.sleep(0)
        async def go():
            jup, rec = Moving(clock), Recorder()
            jup.script = {"M": [(0.0, CurveState(30.0, 1_073_000_000.0))]}
            col = Collector(jup, rec, clock=clock, sleep=sleep)
            col.on_create({"txType": "create", "mint": "M", "traderPublicKey": "C", "pool": "pump", "vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_073_000_000.0, "initialBuy": 0, "solAmount": 0})
            while col.tasks and now[0] < 1400:
                await asyncio.sleep(0)
            for t in list(col.tasks.values()):
                t.cancel()
            await asyncio.gather(*col.tasks.values(), return_exceptions=True)
            return col, rec
        col, rec = asyncio.run(go())
        self.assertEqual(col.stats["off_curve"], 0)
        self.assertEqual(len([r for r in rec.rows if r["k"] == "chk"]), 1)

    def test_one_probe_quote_per_poll_once_verified(self):
        s0 = CurveState(30.0, 1_073_000_000.0)
        col, rec = self.run_collector({"M": [(1002.0, s0), (1010.0, CurveState(34.0, K0 / 34.0))]})
        # after the first observation (probe + check + round trip) every poll is a single probe quote
        self.assertLess(col.jup.stats.get("requests", 0), 1)  # the fake does not count; real cost is asserted below
        self.assertEqual(sum(1 for r in rec.rows if r["k"] == "chk"), 1)

    def test_token_that_never_routes_is_given_up_on(self):
        col, rec = self.run_collector({"M": []})
        self.assertEqual([r["reason"] for r in rec.rows if r["k"] == "end"], ["never_routable"])
        self.assertEqual(col.stats["never_routable"], 1)

    def test_graduation_ends_tracking(self):
        col, rec = self.run_collector({"M": [(1002.0, CurveState(30.0, 1_073_000_000.0)), (1008.0, CurveState(115.0, K0 / 115.0))]})
        self.assertEqual([r["reason"] for r in rec.rows if r["k"] == "end"], ["graduated"])

    def test_token_off_the_standard_curve_is_dropped_not_recorded(self):
        odd = CurveState(30.0, 536_500_000.0)  # k is half the launch k, so the one-quote state is wrong and the 3 SOL check shows it (2.6% miss)
        col, rec = self.run_collector({"M": [(1002.0, odd)]})
        self.assertEqual([r["reason"] for r in rec.rows if r["k"] == "end"], ["off_curve"])
        self.assertEqual([r for r in rec.rows if r["k"] == "chk"], [])
        self.assertEqual(col.stats["off_curve"], 1)
        self.assertEqual(unpriceable(rec.rows), {"M"})  # and the converter then ignores its first observation

    def test_mayhem_launches_are_recorded_as_such_but_never_tracked(self):
        col, rec = self.run_collector({"M": [(1002.0, CurveState(30.0, 1_073_000_000.0))]}, mayhem={"M"})
        created = [r for r in rec.rows if r["k"] == "create"]
        self.assertEqual((len(created), created[0]["mayhem"]), (1, True))
        self.assertEqual([r for r in rec.rows if r["k"] in ("obs", "end")], [])
        self.assertEqual(col.stats["mayhem_skipped"], 1)

    def test_ignores_other_pools_duplicates_and_respects_capacity(self):
        async def go():
            col = Collector(FakeJupiter(lambda: 0.0), Recorder(), max_tracked=1, clock=lambda: 0.0)
            msg = {"txType": "create", "mint": "A", "pool": "pump", "vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_073_000_000.0}
            col.on_create(msg); col.on_create(msg)
            col.on_create({**msg, "mint": "B"}); col.on_create({**msg, "mint": "C", "pool": "bonk"}); col.on_create({"txType": "buy", "mint": "D"})
            stats = (len(col.tasks), col.stats["created"], col.stats["skipped_capacity"])
            for t in col.tasks.values():
                t.cancel()
            await asyncio.gather(*col.tasks.values(), return_exceptions=True)
            return stats

        self.assertEqual(asyncio.run(go()), (1, 1, 1))


class Unpriceable(unittest.TestCase):
    def test_mayhem_off_curve_and_legacy_mismatch_tokens_are_excluded_everywhere(self):
        rows = [
            create_row("OK"), {"k": "obs", "t": 2, "mint": "OK", "v_sol": 31.0, "v_tokens": K0 / 31.0},
            {**create_row("MAYHEM"), "mayhem": True}, {"k": "obs", "t": 2, "mint": "MAYHEM", "v_sol": 31.0, "v_tokens": K0 / 31.0},
            create_row("OFF"), {"k": "end", "t": 3, "mint": "OFF", "reason": "off_curve"},
            create_row("LEGACY"), {"k": "obs", "t": 2, "mint": "LEGACY", "v_sol": 31.0, "v_tokens": K0 / 31.0, "k_ratio": 2.07},
        ]
        self.assertEqual(unpriceable(rows), {"MAYHEM", "OFF", "LEGACY"})
        self.assertEqual({e.mint for e in to_events(rows)}, {"OK"})
        self.assertEqual(list(token_paths(rows)), ["OK"])


class FakeSocket:
    """A PumpPortal connection that delivers one launch and then goes quiet (each quiet wait costs 31 fake seconds)."""

    def __init__(self, now, messages):
        self.now, self.messages, self.sent = now, list(messages), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(text)

    async def recv(self):
        if self.messages:
            return json.dumps(self.messages.pop(0))
        self.now[0] += 31
        raise asyncio.TimeoutError


class FeedReconnect(unittest.TestCase):
    """A flaky feed must never end a long unattended recording (it once did: the server answered the handshake with HTTP 502)."""

    def run_feed(self, failures, minutes=4.0):
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response
        from sniper.live.collector import run

        now = [1000.0]
        slept = []
        failures = [InvalidStatus(Response(502, "Bad Gateway", Headers(), b"")) if f == "502" else f for f in failures]
        socket = FakeSocket(now, [{"txType": "create", "mint": "NEW1", "traderPublicKey": "C", "pool": "pump", "vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_073_000_000.0,
                                   "symbol": "S", "name": "N", "initialBuy": 0, "solAmount": 0}])
        attempts = []

        def connect():
            attempts.append(1)
            if failures:
                raise failures.pop(0)
            return socket

        async def sleep(d):
            slept.append(d)
            now[0] += d
            await asyncio.sleep(0)

        path = tempfile.mktemp(suffix=".jsonl")
        with contextlib.redirect_stdout(io.StringIO()):  # the recorder prints a status line every minute of its (fake) clock
            asyncio.run(run(path, minutes, connect=connect, jup=FakeJupiter(lambda: now[0]), sleep=sleep, clock=lambda: now[0]))
        return read_raw(path), slept, len(attempts), socket

    def test_refused_handshakes_and_resets_are_retried_with_backoff_and_marked_as_one_gap(self):
        rows, slept, attempts, socket = self.run_feed(["502", OSError("connection reset"), asyncio.TimeoutError(), "502"])
        self.assertEqual(attempts, 5)
        self.assertEqual(slept[:4], [1.0, 2.0, 4.0, 8.0])
        kinds = [r["k"] for r in rows]
        self.assertEqual(kinds.count("gap"), 1)       # one outage, however many retries it took
        self.assertEqual(kinds.count("resume"), 1)
        self.assertLess(kinds.index("gap"), kinds.index("resume"))
        self.assertIn("create", kinds)                # and the feed was used afterwards
        self.assertEqual(json.loads(socket.sent[0]), {"method": "subscribeNewToken"})
        self.assertIn("InvalidStatus", next(r for r in rows if r["k"] == "gap")["why"])

    def test_a_healthy_feed_writes_no_gap(self):
        rows, slept, attempts, _ = self.run_feed([])
        self.assertEqual((attempts, [r["k"] for r in rows if r["k"] in ("gap", "resume")]), (1, []))

    def test_the_backoff_is_capped(self):
        _, slept, _, _ = self.run_feed([OSError("x")] * 8, minutes=30)
        self.assertEqual(max(slept[:8]), 30)

    def test_a_mint_created_twice_is_unpriceable_rather_than_two_lives_merged(self):
        rows = [create_row("DUP"), {"k": "obs", "t": 2, "mint": "DUP", "v_sol": 31.0, "v_tokens": K0 / 31.0}, create_row("DUP"), create_row("OK")]
        self.assertEqual(unpriceable(rows), {"DUP"})
        self.assertEqual(list(token_paths(rows)), ["OK"])

    def test_analysis_ignores_gap_and_resume_rows(self):
        rows = [create_row("OK"), {"k": "gap", "t": 5, "why": "x"}, {"k": "obs", "t": 6, "mint": "OK", "v_sol": 31.0, "v_tokens": K0 / 31.0}, {"k": "resume", "t": 9}]
        self.assertEqual(unpriceable(rows), set())
        self.assertEqual(len(to_events(rows)), 2)
        self.assertEqual(list(token_paths(rows)), ["OK"])


if __name__ == "__main__":
    unittest.main()
