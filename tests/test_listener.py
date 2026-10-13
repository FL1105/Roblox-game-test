import asyncio
import json
import unittest

from sniper.listener.live import Listener
from sniper.pump.events import NewToken, ScanNote, Trade
from sniper.pump.store import LaunchStore


class FakeWs:
    def __init__(self, messages=()):
        self.sent = []
        self._messages = list(messages)

    async def send(self, s):
        self.sent.append(json.loads(s))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m if isinstance(m, str) else json.dumps(m)


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, ev):
        self.events.append(ev)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


def create(mint="M1", creator="C1", initial_buy=0.0, sol=0.0):
    return {
        "mint": mint, "traderPublicKey": creator, "txType": "create", "initialBuy": initial_buy, "solAmount": sol,
        "vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1.073e9, "name": "n", "symbol": "S", "pool": "pump",
    }


def trade(mint="M1", trader="X", side="buy", v_sol=31.0, v_tokens=1.04e9):
    return {
        "mint": mint, "traderPublicKey": trader, "txType": side, "tokenAmount": 1000, "solAmount": 0.1,
        "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tokens, "pool": "pump",
    }


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(scan_fn=None, **kw):
    sink, store, clock = ListSink(), LaunchStore(), Clock()
    scan_fn = scan_fn or (lambda launch: ("PASS", 3, "ok"))
    return Listener(sink, store, scan_fn, clock=clock, **kw), sink, store, clock


def run(coro):
    return asyncio.run(coro)


class ListenerTests(unittest.TestCase):
    def test_new_token_is_recorded_subscribed_and_scanned(self):
        async def go():
            lst, sink, store, _ = make()
            ws = FakeWs()
            await lst.on_connect(ws)
            await lst.handle(ws, json.dumps(create(initial_buy=5e6, sol=0.2)))
            await lst.drain()
            return lst, sink, store, ws

        lst, sink, store, ws = run(go())
        self.assertEqual(ws.sent[0], {"method": "subscribeNewToken"})
        self.assertEqual(ws.sent[1], {"method": "subscribeTokenTrade", "keys": ["M1"]})
        (launch,) = sink.of(NewToken)
        self.assertEqual((launch.mint, launch.creator, launch.dev_tokens), ("M1", "C1", 5e6))
        (note,) = sink.of(ScanNote)
        self.assertEqual((note.verdict, note.score, note.mint), ("PASS", 3, "M1"))
        self.assertEqual(store.profile("C1", 10**13).launches_total, 1)

    def test_trades_for_tracked_mints_are_recorded_others_ignored(self):
        async def go():
            lst, sink, _, _ = make()
            ws = FakeWs()
            await lst.handle(ws, json.dumps(create()))
            await lst.handle(ws, json.dumps(trade("M1")))
            await lst.handle(ws, json.dumps(trade("UNTRACKED")))
            await lst.drain()
            return sink

        sink = run(go())
        self.assertEqual([t.mint for t in sink.of(Trade)], ["M1"])

    def test_garbage_and_acks_are_ignored(self):
        async def go():
            lst, sink, _, _ = make()
            ws = FakeWs()
            for raw in ("not json", json.dumps({"message": "Successfully subscribed"}), "123", json.dumps([1])):
                await lst.handle(ws, raw)
            return sink, ws

        sink, ws = run(go())
        self.assertEqual((sink.events, ws.sent), ([], []))

    def test_duplicate_create_is_not_double_counted(self):
        async def go():
            lst, sink, _, _ = make()
            ws = FakeWs()
            await lst.handle(ws, json.dumps(create()))
            await lst.handle(ws, json.dumps(create()))
            await lst.drain()
            return sink

        self.assertEqual(len(run(go()).of(NewToken)), 1)

    def test_window_expiry_unsubscribes_and_stores_outcome(self):
        async def go():
            lst, sink, store, clock = make(observe_s=10)
            ws = FakeWs()
            await lst.handle(ws, json.dumps(create(initial_buy=1e8, sol=3)))
            clock.t += 5
            await lst.handle(ws, json.dumps(trade("M1", "C1", "sell", v_sol=30.0, v_tokens=1.07e9)))
            clock.t += 20  # past the window
            await lst.handle(ws, json.dumps(create("M2", "C2")))
            await lst.drain()
            return lst, sink, store, ws

        lst, sink, store, ws = run(go())
        self.assertIn({"method": "unsubscribeTokenTrade", "keys": ["M1"]}, ws.sent)
        self.assertNotIn("M1", lst.trackers)
        self.assertIn("M2", lst.trackers)
        # the creator sold what they bought but the price never rose, so it isn't classed as a rug
        self.assertEqual(store.profile("C1", 10**13).rugs_total, 0)
        self.assertEqual(len(store._db.execute("SELECT * FROM outcomes").fetchall()), 1)

    def test_scan_rate_limit_records_skipped_instead_of_scanning(self):
        calls = []

        def scan(launch):
            calls.append(launch.mint)
            return "PASS", 0, ""

        async def go():
            lst, sink, _, _ = make(scan_fn=scan, max_scans_per_min=2)
            ws = FakeWs()
            for i in range(4):
                await lst.handle(ws, json.dumps(create(f"M{i}", f"C{i}")))
            await lst.drain()
            return lst, sink

        lst, sink = run(go())
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(n.verdict for n in sink.of(ScanNote)), ["PASS", "PASS", "SKIPPED", "SKIPPED"])
        self.assertEqual(lst.stats["skipped_scans"], 2)

    def test_scanner_crash_is_recorded_as_error_and_does_not_stop_the_listener(self):
        def scan(launch):
            raise RuntimeError("rpc exploded")

        async def go():
            lst, sink, _, _ = make(scan_fn=scan)
            ws = FakeWs()
            await lst.handle(ws, json.dumps(create("M1")))
            await lst.handle(ws, json.dumps(create("M2")))
            await lst.drain()
            return sink

        notes = run(go()).of(ScanNote)
        self.assertEqual([n.verdict for n in notes], ["ERROR", "ERROR"])
        self.assertIn("rpc exploded", notes[0].summary)

    def test_reconnect_resubscribes_to_mints_still_being_observed(self):
        async def go():
            lst, _, _, _ = make()
            await lst.handle(FakeWs(), json.dumps(create("M1")))
            await lst.handle(FakeWs(), json.dumps(create("M2", "C2")))
            await lst.drain()
            ws2 = FakeWs()
            await lst.on_connect(ws2)
            return ws2

        ws2 = run(go())
        self.assertEqual(ws2.sent[0], {"method": "subscribeNewToken"})
        self.assertEqual(sorted(ws2.sent[1]["keys"]), ["M1", "M2"])

    def test_run_consumes_a_whole_stream(self):
        async def go():
            lst, sink, _, _ = make()
            ws = FakeWs([create("M1"), trade("M1"), trade("M1", side="sell")])
            await lst.run(ws)
            await lst.drain()
            return lst, sink

        lst, sink = run(go())
        self.assertEqual((lst.stats["launches"], lst.stats["trades"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
