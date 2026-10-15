import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest

from sniper.live.stream import MissingKey, StreamRecorder, run, scrub

KEY = "sk-test-SECRET-key-123"


def create(mint, creator="CREATOR", mayhem=False, pool="pump"):
    return {"txType": "create", "mint": mint, "traderPublicKey": creator, "name": "N", "symbol": "S", "initialBuy": 5e6, "solAmount": 0.2, "pool": pool,
            "vSolInBondingCurve": 30.2, "vTokensInBondingCurve": 1.068e9, "is_mayhem_mode": mayhem, "signature": "sigc"}


def trade(mint, side, who="W1", sol=0.5, tokens=1e7):
    return {"txType": side, "mint": mint, "traderPublicKey": who, "solAmount": sol, "tokenAmount": tokens, "pool": "pump", "vSolInBondingCurve": 30.9, "vTokensInBondingCurve": 1.05e9,
            "signature": "sigt"}


class Socket:
    def __init__(self, now, messages):
        self.now, self.messages, self.sent = now, list(messages), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.messages:
            m = self.messages.pop(0)
            if isinstance(m, tuple):          # ("advance", seconds): time passes between messages
                self.now[0] += m[1]
                return json.dumps({"message": "tick"})
            return m if isinstance(m, str) else json.dumps(m)
        self.now[0] += 31
        raise asyncio.TimeoutError


def run_stream(script, minutes=5.0, key=KEY, **kw):
    """`script` is a list of connection attempts: an Exception to raise, or a list of messages to deliver."""
    now = [1000.0]
    sockets, attempts = [], []

    def connect():
        attempts.append(1)
        step = script[min(len(attempts) - 1, len(script) - 1)]
        if isinstance(step, Exception):
            raise step
        sock = Socket(now, step)
        sockets.append(sock)
        return sock

    async def sleep(d):
        now[0] += d
        await asyncio.sleep(0)

    path = tempfile.mktemp(suffix=".jsonl")
    out = io.StringIO()
    err = None
    with contextlib.redirect_stdout(out):
        try:
            asyncio.run(run(path, minutes, key, connect=connect, sleep=sleep, clock=lambda: now[0], **kw))
        except MissingKey as e:
            err = e
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    return rows, sockets, out.getvalue(), err, len(attempts)


class Recording(unittest.TestCase):
    def test_launches_and_every_trade_with_its_wallet_are_recorded(self):
        rows, sockets, _, err, _ = run_stream([[create("A"), trade("A", "buy", "W1"), trade("A", "sell", "W2", 0.4), create("B", mayhem=True), trade("B", "buy", "W3")]])
        self.assertIsNone(err)
        kinds = [r["k"] for r in rows]
        self.assertEqual(kinds, ["create", "trade", "trade", "create", "trade"])
        self.assertEqual((rows[1]["trader"], rows[1]["side"], rows[1]["sol"], rows[1]["v_sol"]), ("W1", "buy", 0.5, 30.9))
        self.assertEqual((rows[2]["trader"], rows[2]["side"]), ("W2", "sell"))
        self.assertEqual((rows[0]["creator"], rows[0]["dev_sol"], rows[0]["mayhem"]), ("CREATOR", 0.2, False))
        self.assertTrue(rows[3]["mayhem"])
        sent = [m for m in sockets[0].sent]
        self.assertEqual(sent[0], {"method": "subscribeNewToken"})
        self.assertEqual([m["keys"] for m in sent if m["method"] == "subscribeTokenTrade"], [["A"], ["B"]])

    def test_trades_for_tokens_we_do_not_follow_other_pools_and_junk_are_ignored(self):
        rows, _, _, _, _ = run_stream([[trade("UNKNOWN", "buy"), create("X", pool="raydium"), "not json", {"txType": "buy"}, {"nonsense": 1}, create("A"), {"txType": "buy", "mint": "A"}]])
        self.assertEqual([r["k"] for r in rows], ["create"])

    def test_a_token_is_unfollowed_after_the_observation_window(self):
        rows, sockets, _, _, _ = run_stream([[create("A"), ("advance", 700), trade("A", "buy")]], minutes=30)
        self.assertEqual([m["keys"] for m in sockets[0].sent if m["method"] == "unsubscribeTokenTrade"], [["A"]])
        self.assertEqual([r["k"] for r in rows], ["create"])   # the trade after the window is not recorded


class Outages(unittest.TestCase):
    def test_outage_then_recovery_writes_gap_resume_and_resubscribes(self):
        now = [1000.0]
        attempts = []
        sent_all = []

        class Dropping(Socket):
            async def recv(self):
                if self.messages:
                    return json.dumps(self.messages.pop(0))
                raise OSError("connection lost")

        def connect():
            attempts.append(1)
            n = len(attempts)
            if n == 2:
                raise OSError("still down")
            sock = (Dropping if n == 1 else Socket)(now, [create("A"), trade("A", "buy")] if n == 1 else [trade("A", "sell", "W9")])
            sent_all.append(sock)
            return sock

        async def sleep(d):
            now[0] += d
            await asyncio.sleep(0)

        path = tempfile.mktemp(suffix=".jsonl")
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(run(path, 3.0, KEY, connect=connect, sleep=sleep, clock=lambda: now[0]))
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        kinds = [r["k"] for r in rows]
        self.assertEqual(kinds.count("gap"), 1)
        self.assertEqual(kinds.count("resume"), 1)
        self.assertLess(kinds.index("gap"), kinds.index("resume"))
        self.assertIn("W9", [r.get("trader") for r in rows])        # the trade after the outage was recorded
        second = sent_all[1].sent
        self.assertEqual([m for m in second if m["method"] == "subscribeTokenTrade"], [{"method": "subscribeTokenTrade", "keys": ["A"]}])


class KeyHandling(unittest.TestCase):
    def test_no_key_means_no_run(self):
        saved = os.environ.pop("PUMPPORTAL_API_KEY", None)
        try:
            with self.assertRaises(MissingKey):
                asyncio.run(run(tempfile.mktemp(suffix=".jsonl"), 1.0, ""))
        finally:
            if saved:
                os.environ["PUMPPORTAL_API_KEY"] = saved

    def test_the_key_never_reaches_the_file_or_the_log_even_when_an_error_contains_it(self):
        rows, _, out, err, _ = run_stream([OSError(f"cannot connect to wss://pumpportal.fun/api/data?api-key={KEY}"), [create("A")]])
        self.assertNotIn(KEY, out)
        self.assertNotIn(KEY, json.dumps(rows))
        self.assertIn("***", out)
        self.assertEqual(scrub(f"x{KEY}y", KEY), "x***y")

    def test_a_feed_that_keeps_refusing_the_trade_stream_stops_with_a_clear_message(self):
        refusal = {"message": "'subscribeTokenTrade' and 'subscribeAccountTrade' methods are only available when connecting with an API key funded with at least 0.02 SOL."}
        rows, _, out, err, _ = run_stream([[create("A"), refusal, refusal, refusal]])
        self.assertIsInstance(err, MissingKey)
        self.assertIn("refuses the trade feed", str(err))

    def test_the_recorder_can_only_read(self):
        """It sends subscribe/unsubscribe messages and has no code path that could spend: checked on what is sent and on the source."""
        _, sockets, _, _, _ = run_stream([[create("A"), trade("A", "buy"), ("advance", 700)]], minutes=30)
        methods = {m["method"] for s in sockets for m in s.sent}
        self.assertLessEqual(methods, {"subscribeNewToken", "subscribeTokenTrade", "unsubscribeTokenTrade"})
        with open(os.path.join(os.path.dirname(__file__), "..", "sniper", "live", "stream.py")) as fh:
            src = fh.read().lower()
        # the file's own description says it never sends a transaction, so look for the machinery, not the words
        for forbidden in ("trade-local", "trade?api-key", "/api/trade", "sendtransaction", "signtransaction", "private_key", "privatekey", "keypair", "action\": \"buy", "\"action\""):
            self.assertNotIn(forbidden, src, forbidden)


class RecorderUnit(unittest.TestCase):
    def test_duplicate_creates_are_recorded_once(self):
        path = tempfile.mktemp(suffix=".jsonl")
        rec = StreamRecorder(path)
        self.assertEqual(rec.on_message(create("A")), "A")
        self.assertIsNone(rec.on_message(create("A")))
        rec.close()
        with open(path) as f:
            self.assertEqual(len(f.readlines()), 1)


if __name__ == "__main__":
    unittest.main()


class Conversion(unittest.TestCase):
    """Stream recordings feed the same paper engine, replay and analysis as the price-only ones, and add what they could not: who traded."""

    def rows(self):
        k0 = 30.2 * 1.068e9
        return [
            {"k": "create", "t": 1000, "mayhem": False, "mint": "A", "creator": "DEV", "symbol": "S", "name": "N", "dev_tokens": 5e6, "dev_sol": 0.2, "v_sol": 30.2, "v_tokens": 1.068e9, "sig": "s0"},
            {"k": "trade", "t": 1100, "mint": "A", "trader": "W1", "side": "buy", "sol": 1.0, "tokens": 3.0e7, "v_sol": 31.2, "v_tokens": k0 / 31.2, "sig": "s1"},
            {"k": "trade", "t": 1200, "mint": "A", "trader": "DEV", "side": "sell", "sol": 0.3, "tokens": 5e6, "v_sol": 30.9, "v_tokens": k0 / 30.9, "sig": "s2"},
            {"k": "gap", "t": 1250, "why": "x"}, {"k": "resume", "t": 1300},
        ]

    def test_trades_keep_their_wallet_and_launches_get_a_scan_note(self):
        from sniper.live.convert import to_events, token_paths
        from sniper.pump.events import NewToken, ScanNote, Trade
        ev = to_events(self.rows())
        self.assertEqual([type(e).__name__ for e in ev], ["NewToken", "ScanNote", "Trade", "Trade"])
        self.assertEqual([(e.trader, e.side) for e in ev if isinstance(e, Trade)], [("W1", "buy"), ("DEV", "sell")])
        note = next(e for e in ev if isinstance(e, ScanNote))
        self.assertEqual(note.verdict, "PASS")
        self.assertIn("no sell-route check", note.summary)
        path = token_paths(self.rows())["A"]
        self.assertEqual(len(path["trades"]), 2)
        self.assertEqual([o["t"] for o in path["obs"]], [1100, 1200])

    def test_the_creator_selling_is_visible_to_the_engine_so_the_exit_fires(self):
        from sniper.live.convert import to_events
        from sniper.paper.engine import PaperEngine, Strategy
        engine = PaperEngine(Strategy(latency_ms=0, tx_cost_sol=0.0, take_profit_pct=1000, stop_loss_pct=99, exit_on_creator_sell=True, max_dev_buy_pct=100), 1.0)
        rows = self.rows()
        rows.insert(2, {"k": "trade", "t": 1150, "mint": "A", "trader": "W2", "side": "buy", "sol": 0.01, "tokens": 3e5, "v_sol": 31.21, "v_tokens": 30.2 * 1.068e9 / 31.21, "sig": "s1b"})
        for e in to_events(rows):
            engine.on_event(e)
        result = engine.finish()
        self.assertEqual([c.reason for c in result.closed], ["creator_sold"])

    def test_price_only_recordings_are_unchanged(self):
        from sniper.live.convert import to_events
        rows = [{"k": "create", "t": 1, "mayhem": False, "mint": "P", "creator": "C", "symbol": "S", "name": "N", "dev_tokens": 0.0, "dev_sol": 0.0, "v_sol": 30.0, "v_tokens": 1.073e9}]
        self.assertEqual([type(e).__name__ for e in to_events(rows)], ["NewToken"])
