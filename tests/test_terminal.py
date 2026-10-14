import json
import math
import re
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from sniper.paper.engine import PaperEngine, Strategy
from sniper.pump import synth
from sniper.pump.events import NewToken
from sniper.terminal.feeds import DemoFeed, ReplayFeed, repeat_lap, start_feed
from sniper.terminal.server import INDEX, make_server
from sniper.terminal.state import DEFAULT_ROWS, MAX_ROWS, TerminalState, clamp_rows, default_strategy, parse_settings, settings_dict

from .sim import Sim

FAST = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1)


def winner_events(mint="MINT1", t0=1000):
    sim = Sim(mint, "CREATOR", t0)
    return [sim.create(), sim.scan(t0 + 100), sim.buy(t0 + 200, 1.0), sim.buy(t0 + 300, 12.0), sim.buy(t0 + 400, 0.5)]


class Settings(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        s = default_strategy()
        self.assertEqual(parse_settings(settings_dict(s), s), s)

    def test_partial_update_changes_only_what_was_sent(self):
        s = default_strategy()
        new = parse_settings({"take_profit": 45, "verdicts": ["PASS"]}, s)
        self.assertEqual((new.take_profit_pct, new.allowed_verdicts), (45.0, frozenset({"PASS"})))
        self.assertEqual(new.stop_loss_pct, s.stop_loss_pct)

    def test_amount_range_maps_to_caution_and_pass_sizes(self):
        new = parse_settings({"amount_min": 0.02, "amount_max": 0.3}, default_strategy())
        self.assertEqual((new.size_caution_sol, new.size_sol), (0.02, 0.3))

    def test_rejects_bad_input(self):
        s = default_strategy()
        bad = [
            {"amount_min": 1, "amount_max": 0.5}, {"stop_loss": 100}, {"stop_loss": 0}, {"take_profit": True},
            {"take_profit": "30"}, {"take_profit": math.nan}, {"take_profit": math.inf}, {"nonsense": 1},
            {"verdicts": []}, {"verdicts": ["REJECT"]}, {"verdicts": "PASS"}, {"exit_on_creator_sell": 1},
            {"max_open": 0}, {"latency_ms": -1}, {"tx_cost_sol": 5},
        ]
        for payload in bad:
            with self.assertRaises(ValueError, msg=str(payload)):
                parse_settings(payload, s)


class State(unittest.TestCase):
    def feed(self, state, events):
        for ev in events:
            state.emit(ev)

    def test_stopped_bot_watches_but_never_trades(self):
        st = TerminalState(FAST)
        self.feed(st, winner_events())
        snap = st.snapshot()
        self.assertEqual(snap["stats"]["trades"], 0)
        self.assertEqual(snap["counters"]["skipped"], {"bot_stopped": 1})
        self.assertEqual((snap["rows"][0]["status"], snap["rows"][0]["detail"]), ("skipped", "bot_stopped"))

    def test_running_bot_trades_and_rows_follow_the_lifecycle(self):
        st = TerminalState(FAST)
        st.set_running(True)
        events = winner_events()
        st.emit(events[0])
        self.assertEqual(st.snapshot()["rows"][0]["status"], "scanning")
        st.emit(events[1])
        self.assertEqual(st.snapshot()["rows"][0]["status"], "entering")
        st.emit(events[2])
        row = st.snapshot()["rows"][0]
        self.assertEqual((row["status"], row["amount"]), ("holding", 0.1))
        self.feed(st, events[3:])
        snap = st.snapshot()
        row = snap["rows"][0]
        self.assertEqual((row["status"], row["detail"]), ("closed", "take_profit"))
        self.assertGreater(row["pnl_sol"], 0)
        self.assertEqual((snap["stats"]["trades"], snap["stats"]["wins"], snap["stats"]["win_rate"]), (1, 1, 100.0))
        self.assertAlmostEqual(snap["paper"]["equity"], 1.0 + row["pnl_sol"])

    def test_stopping_keeps_managing_open_positions(self):
        st = TerminalState(FAST)
        st.set_running(True)
        events = winner_events()
        self.feed(st, events[:3])
        st.set_running(False)
        self.feed(st, events[3:])
        self.assertEqual(st.snapshot()["stats"]["trades"], 1)

    def test_settings_apply_to_the_live_engine(self):
        st = TerminalState(FAST)
        st.update_settings({"take_profit": 77})
        self.assertEqual(st.engine.s.take_profit_pct, 77.0)

    def test_reset_clears_account_tokens_and_logs(self):
        st = TerminalState(FAST)
        st.set_running(True)
        self.feed(st, winner_events())
        st.reset()
        snap = st.snapshot()
        self.assertEqual((snap["rows"], snap["stats"]["trades"], snap["paper"]["equity"]), ([], 0, 1.0))
        self.assertTrue(st.running)  # reset doesn't flip the bot on or off

    def test_window_filter_excludes_trades_older_than_24h(self):
        st = TerminalState(FAST)
        st.set_running(True)
        self.feed(st, winner_events("A", 1000))
        day = 86_400_000
        self.feed(st, [NewToken(1000 + 2 * day, "Z", "C", "z", "Z", 0, 0, 30.0, 1.073e9)])
        self.assertEqual(st.snapshot("24h")["stats"]["trades"], 0)
        self.assertEqual(st.snapshot("all")["stats"]["trades"], 1)

    def test_token_names_pass_through_verbatim_for_the_ui_to_escape(self):
        st = TerminalState(FAST)
        evil = NewToken(1, "M", "C", "<img src=x onerror=alert(1)>", "<script>", 0, 0, 30.0, 1.073e9)
        st.emit(evil)
        self.assertEqual(st.snapshot()["rows"][0]["symbol"], "<script>")

    def test_log_is_capped_and_newest_first(self):
        st = TerminalState(FAST, log_size=5)
        for i in range(20):
            st.emit(NewToken(i, f"M{i}", "C", "n", f"S{i}", 0, 0, 30.0, 1.073e9))
        snap = st.snapshot()
        self.assertEqual((len(snap["logs"]), snap["log_total"]), (5, 20))
        self.assertIn("S19", snap["logs"][0]["text"])


class Frontend(unittest.TestCase):
    def test_innerhtml_is_only_used_for_static_icons(self):
        src = INDEX.read_text()
        uses = re.findall(r"^.*innerHTML.*$", src, re.M)
        self.assertTrue(uses)
        for line in uses:
            self.assertTrue("svg(" in line or "<svg" in line, f"untrusted innerHTML? {line.strip()}")
        self.assertNotIn("insertAdjacentHTML", src)
        self.assertNotIn("document.write", src)

    def test_page_never_asks_for_wallet_or_keys(self):
        src = INDEX.read_text().lower()
        for word in ("seed phrase", "private key", "connect wallet", "signtransaction", "window.solana", "phantom"):
            self.assertNotIn(word, src.replace("no wallet, private key or signature is ever requested here", ""))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = TerminalState(FAST)
        cls.srv = make_server(cls.state, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, body=None, headers=None, method=None, raw=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_index_served_with_security_headers(self):
        code, body, headers = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Launch Sniper", body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_state_endpoint_shape(self):
        code, body, _ = self.req("/api/state")
        snap = json.loads(body)
        self.assertEqual(code, 200)
        for key in ("running", "simulated", "settings", "stats", "rows", "logs", "equity_curve", "paper", "counters"):
            self.assertIn(key, snap)

    def test_bot_toggle(self):
        self.assertEqual(json.loads(self.req("/api/bot", {"running": True})[1]), {"running": True})
        self.assertTrue(self.state.running)
        self.req("/api/bot", {"running": False})
        self.assertFalse(self.state.running)
        self.assertEqual(self.req("/api/bot", {"running": "yes"})[0], 400)

    def test_settings_valid_and_invalid(self):
        code, body, _ = self.req("/api/settings", {"stop_loss": 15})
        self.assertEqual((code, json.loads(body)["settings"]["stop_loss"]), (200, 15.0))
        code, body, _ = self.req("/api/settings", {"stop_loss": 500})
        self.assertEqual(code, 400)
        self.assertIn("Stop loss", json.loads(body)["error"])
        self.assertEqual(self.state.engine.s.stop_loss_pct, 15.0)  # the bad update changed nothing

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.req("/api/state", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.req("/api/bot", {"running": True}, headers={"Host": "evil.example.com:80"})[0], 403)
        self.assertFalse(self.state.running)

    def test_non_json_posts_are_refused(self):
        self.assertEqual(self.req("/api/bot", raw=b"running=true", headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 415)
        self.assertEqual(self.req("/api/bot", raw=b"running=true", headers={"Content-Type": "text/plain"})[0], 415)

    def test_malformed_and_oversized_bodies(self):
        self.assertEqual(self.req("/api/bot", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("/api/bot", raw=b"[1,2]")[0], 400)
        big = json.dumps({"running": True, "pad": "x" * 70_000}).encode()  # valid JSON, just too large
        self.assertEqual(self.req("/api/bot", raw=big)[0], 400)
        self.assertFalse(self.state.running)

    def test_unknown_paths(self):
        self.assertEqual(self.req("/nope")[0], 404)
        self.assertEqual(self.req("/api/nope", {})[0], 404)

    def test_there_is_no_endpoint_that_could_move_funds(self):
        for path in ("/api/trade", "/api/wallet", "/api/connect", "/api/withdraw"):
            self.assertEqual(self.req(path, {})[0], 404)


class Feeds(unittest.TestCase):
    def test_replay_orders_events_and_caps_sleeps(self):
        slept, got = [], []
        events = list(reversed(winner_events()))
        ReplayFeed(events, speed=2.0, max_gap_s=0.05, sleep=slept.append).run(got.append, threading.Event())
        self.assertEqual([e.t for e in got], sorted(e.t for e in events))
        self.assertTrue(slept and all(s <= 0.05 for s in slept))

    def test_replay_stops_when_asked(self):
        stop, got = threading.Event(), []
        def emit(ev):
            got.append(ev)
            stop.set()
        ReplayFeed(winner_events(), sleep=lambda s: None).run(emit, stop)
        self.assertEqual(len(got), 1)

    def test_demo_feed_is_deterministic_and_endless(self):
        def collect(n):
            stop, got = threading.Event(), []
            def emit(ev):
                got.append(ev)
                if len(got) >= n:
                    stop.set()
            DemoFeed(seed=3, batch=5, sleep=lambda s: None, now_ms=lambda: 1_000_000).run(emit, stop)
            return got
        a, b = collect(400), collect(400)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 400)  # more than one batch of 5 tokens, so the feed keeps going

    def test_finished_and_failed_feeds_are_visible(self):
        st = TerminalState(FAST)
        start_feed(ReplayFeed(winner_events(), sleep=lambda s: None), st, threading.Event()).join(2)
        self.assertEqual(st.feed_status, "finished")

        class Broken:
            def run(self, emit, stop):
                raise RuntimeError("boom")
        st2 = TerminalState(FAST)
        start_feed(Broken(), st2, threading.Event()).join(2)
        self.assertTrue(st2.feed_status.startswith("error: RuntimeError: boom"))


class SyntheticMarket(unittest.TestCase):
    def test_generated_market_is_valid(self):
        events = synth.generate(1, 150, 1_000)
        self.assertEqual([e.t for e in events], sorted(e.t for e in events))
        self.assertTrue(all(e.v_sol > 0 and e.v_tokens > 0 for e in events if hasattr(e, "v_sol")))
        launches = [e for e in events if isinstance(e, NewToken)]
        scans = {e.mint for e in events if hasattr(e, "verdict")}
        self.assertEqual(len(launches), 150)
        self.assertEqual({l.mint for l in launches}, scans)
        self.assertEqual(synth.generate(1, 20, 1_000), synth.generate(1, 20, 1_000))
        self.assertNotEqual(synth.generate(1, 20, 1_000), synth.generate(2, 20, 1_000))

    def test_mix_contains_rugs_and_graduations(self):
        events = synth.generate(5, 300, 1_000)
        creators = {e.mint: e.creator for e in events if isinstance(e, NewToken)}
        self.assertTrue(any(getattr(e, "side", "") == "sell" and e.trader == creators[e.mint] for e in events))
        self.assertTrue(any(getattr(e, "v_tokens", 1e18) <= 280_000_000 for e in events))


class EngineHooks(unittest.TestCase):
    def run_events(self, events, strategy, enabled=True):
        notes = []
        eng = PaperEngine(strategy, on_note=lambda *a: notes.append(a))
        eng.enabled = enabled
        for ev in events:
            eng.on_event(ev)
        eng.finish()
        return eng, notes

    def test_notes_cover_entry_exit_and_skips(self):
        _, notes = self.run_events(winner_events(), FAST)
        self.assertEqual([n[1] for n in notes], ["entry", "exit"])
        _, notes = self.run_events(winner_events(), FAST, enabled=False)
        self.assertEqual([(n[1], n[3]) for n in notes], [("skip", "skipped (bot_stopped)")])

    def test_caution_verdict_uses_the_smaller_size(self):
        sim = Sim()
        events = [sim.create(), sim.scan(1100, "CAUTION"), sim.buy(1200, 0.2)]
        eng, _ = self.run_events(events, replace(FAST, size_sol=0.2, size_caution_sol=0.05))
        self.assertAlmostEqual(eng.closed[0].sol_in, 0.05)
        sim = Sim()
        events = [sim.create(), sim.scan(1100, "PASS"), sim.buy(1200, 0.2)]
        eng, _ = self.run_events(events, replace(FAST, size_sol=0.2, size_caution_sol=0.05))
        self.assertAlmostEqual(eng.closed[0].sol_in, 0.2)




class ConfigOptions(unittest.TestCase):
    def test_every_preset_is_valid_and_the_steady_one_matches_the_users_goal(self):
        from sniper.terminal.state import PRESETS
        base = default_strategy()
        for name, preset in PRESETS.items():
            new = parse_settings(preset["settings"], base)
            self.assertLessEqual(new.size_caution_sol, new.size_sol, name)
        steady = parse_settings(PRESETS["steady"]["settings"], base)
        self.assertEqual((steady.take_profit_pct, steady.allowed_verdicts), (30.0, frozenset({"PASS"})))
        self.assertEqual([k for k, p in PRESETS.items() if p["tag"] == "RECOMMENDED"], ["steady"])

    def test_new_options_round_trip_through_settings(self):
        s = parse_settings({
            "trailing_stop_pct": 12, "slippage_pct": 7, "min_liquidity_sol": 3, "size_mode": "random",
            "auto_compound": False, "use_blacklist": False, "honeypot_detection": False,
            "blacklist": ["7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"],
        }, default_strategy())
        self.assertEqual((s.trailing_stop_pct, s.slippage_pct, s.min_liquidity_sol, s.size_mode), (12.0, 7.0, 3.0, "random"))
        self.assertEqual((s.auto_compound, s.use_blacklist, s.require_scan), (False, False, False))
        self.assertEqual(parse_settings(settings_dict(s), s), s)

    def test_rejects_bad_new_options(self):
        s = default_strategy()
        for payload in ({"size_mode": "yolo"}, {"slippage_pct": 0}, {"slippage_pct": 51}, {"trailing_stop_pct": 100},
                        {"min_liquidity_sol": -1}, {"blacklist": ["nope"]}, {"blacklist": "x"}, {"blacklist": [1]},
                        {"blacklist": ["7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"] * 201},
                        {"honeypot_detection": "yes"}, {"auto_compound": 1}):
            with self.assertRaises(ValueError, msg=str(payload)):
                parse_settings(payload, s)

    def test_honeypot_detection_off_buys_without_waiting_for_a_scan(self):
        st = TerminalState(replace(FAST, require_scan=False))
        st.set_running(True)
        sim = Sim()
        for ev in [sim.create(), sim.buy(1100, 1.0)]:
            st.emit(ev)
        self.assertEqual(st.snapshot()["rows"][0]["status"], "holding")

    def test_blacklist_reaches_the_engine_and_blocks_the_creator(self):
        st = TerminalState(FAST)
        st.set_running(True)
        wallet = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        st.update_settings({"blacklist": [wallet]})
        sim = Sim("M1", wallet)
        for ev in [sim.create(), sim.scan(1100), sim.buy(1200, 0.5)]:
            st.emit(ev)
        row = st.snapshot()["rows"][0]
        self.assertEqual((row["status"], row["detail"]), ("skipped", "blacklisted"))

    def test_reset_can_change_the_paper_bankroll(self):
        st = TerminalState(FAST)
        st.reset(25)
        self.assertEqual((st.snapshot()["paper"]["start"], st.snapshot()["paper"]["equity"]), (25.0, 25.0))
        for bad in (0, -5, 10_000, True, "5", math.nan):
            with self.assertRaises(ValueError, msg=str(bad)):
                st.reset(bad)
        self.assertEqual(st.snapshot()["paper"]["start"], 25.0)

    def test_state_endpoint_ships_presets(self):
        self.assertIn("steady", TerminalState(FAST).snapshot()["presets"])

    def test_config_screen_has_the_three_real_tabs_and_no_fake_ai_switches(self):
        src = INDEX.read_text()
        for tab in ('data-pane="trading"', 'data-pane="safety"', 'data-pane="advanced"', "Save configuration"):
            self.assertIn(tab, src)
        self.assertNotIn('k: "ai_', src)
        self.assertIn("Left out on purpose", src)


class ConfigServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = TerminalState(FAST)
        cls.srv = make_server(cls.state, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def post(self, path, body):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_reset_with_bankroll_over_http(self):
        self.assertEqual(self.post("/api/reset", {"start_sol": 12})[1]["start_sol"], 12.0)
        code, body = self.post("/api/reset", {"start_sol": 0})
        self.assertEqual(code, 400)
        self.assertIn("bankroll", body["error"])

    def test_full_configuration_saves_atomically(self):
        before = settings_dict(self.state.strategy)
        code, _ = self.post("/api/settings", {"take_profit": 55, "slippage_pct": 999})
        self.assertEqual(code, 400)
        self.assertEqual(settings_dict(self.state.strategy), before)  # nothing half-applied


class LadderSettingsAndDisplay(unittest.TestCase):
    RUNGS = [{"gain_pct": 30, "sell_pct": 50}, {"gain_pct": 60, "sell_pct": 25}]

    def test_round_trip_and_defaults(self):
        s = parse_settings({"ladder": self.RUNGS, "ladder_breakeven_stop": False}, default_strategy())
        self.assertEqual(s.ladder, ((30.0, 50.0), (60.0, 25.0)))
        self.assertEqual(parse_settings(settings_dict(s), s), s)
        self.assertEqual(settings_dict(default_strategy())["ladder"], [])  # off unless asked for
        self.assertTrue(settings_dict(default_strategy())["ladder_breakeven_stop"])

    def test_rejects_malformed_ladders(self):
        s = default_strategy()
        bad = [
            "x", [30], [{"gain_pct": 30}], [{"gain_pct": 30, "sell_pct": 50, "extra": 1}], [{"gain_pct": "30", "sell_pct": 50}],
            [{"gain_pct": True, "sell_pct": 50}], [{"gain_pct": 0, "sell_pct": 50}], [{"gain_pct": 1001, "sell_pct": 50}],
            [{"gain_pct": 30, "sell_pct": 0}], [{"gain_pct": 30, "sell_pct": 101}],
            [{"gain_pct": 60, "sell_pct": 50}, {"gain_pct": 30, "sell_pct": 25}], [{"gain_pct": 30, "sell_pct": 50}, {"gain_pct": 30, "sell_pct": 25}],
            [{"gain_pct": 30, "sell_pct": 60}, {"gain_pct": 60, "sell_pct": 41}], [{"gain_pct": g, "sell_pct": 5} for g in range(10, 70, 10)],
        ]
        for ladder in bad:
            with self.assertRaises(ValueError, msg=str(ladder)):
                parse_settings({"ladder": ladder}, s)

    def test_error_messages_are_specific_enough_to_act_on(self):
        s = default_strategy()
        for ladder, text in (([{"gain_pct": 60, "sell_pct": 50}, {"gain_pct": 30, "sell_pct": 25}], "go up"), ([{"gain_pct": 30, "sell_pct": 70}, {"gain_pct": 60, "sell_pct": 40}], "more than 100%"), ([{"gain_pct": 0, "sell_pct": 5}], "rung 1")):
            with self.assertRaisesRegex(ValueError, text):
                parse_settings({"ladder": ladder}, s)

    def test_table_shows_rung_progress_and_realised_profit_then_the_books_balance(self):
        st = TerminalState(replace(FAST, max_hold_s=3600), start_sol=1.0)
        st.update_settings({"ladder": self.RUNGS, "stop_loss": 40, "take_profit": 30})
        st.set_running(True)
        x = Sim("M1", "C", 1_000_000)
        for ev in [x.create(), x.scan(1_000_100), x.buy(1_000_200, 0.01), x.buy(1_001_000, 5.0), x.buy(1_002_000, 0.01)]:
            st.emit(ev)
        row = st.snapshot()["rows"][0]
        self.assertEqual((row["status"], row["detail"]), ("holding", "rung 1/2"))
        self.assertAlmostEqual(row["amount"], 0.1)                       # still shown against what was originally paid
        self.assertGreater(row["pnl_sol"], 0)
        self.assertTrue(any("ladder rung 1" in l["text"] for l in st.snapshot()["logs"]))
        for ev in [x.buy(1_003_000, 5.0), x.buy(1_004_000, 0.01)]:
            st.emit(ev)
        st.engine.finish()
        snap = st.snapshot("all")
        self.assertAlmostEqual(snap["paper"]["equity"], 1.0 + snap["stats"]["pnl_sol"], places=9)
        self.assertEqual(snap["stats"]["trades"], 1)  # one position, however many rungs it sold in


if __name__ == "__main__":
    unittest.main()


class RowLimit(unittest.TestCase):
    """The dashboard only gets as many token rows as it asks for, so a busy market can't bog the page down."""

    def busy_state(self, n=60, running=False):
        st = TerminalState(FAST)
        if running:
            st.set_running(True)
        for i in range(n):
            st.emit(Sim(f"MINT{i:03d}", f"CREATOR{i:03d}", 1000 + i * 10).create())
        return st

    def test_clamp(self):
        self.assertEqual([clamp_rows(v) for v in (None, "", "abc", "inf", 0, -5, 7, "12", 12.9, 10**9, True)],
                         [DEFAULT_ROWS, DEFAULT_ROWS, DEFAULT_ROWS, DEFAULT_ROWS, 1, 1, 7, 12, 12, MAX_ROWS, 1])

    def test_default_is_small_and_newest_first(self):
        snap = self.busy_state().snapshot()
        self.assertEqual(len(snap["rows"]), DEFAULT_ROWS)
        self.assertEqual(snap["rows_total"], 60)
        self.assertEqual([r["mint"] for r in snap["rows"][:2]], ["MINT059", "MINT058"])
        self.assertEqual(snap["rows"][-1]["mint"], f"MINT{60 - DEFAULT_ROWS:03d}")

    def test_limit_is_respected_and_capped(self):
        st = self.busy_state(150)
        self.assertEqual(len(st.snapshot(rows=10)["rows"]), 10)
        self.assertEqual(len(st.snapshot(rows=1)["rows"]), 1)
        self.assertEqual(len(st.snapshot(rows=100000)["rows"]), MAX_ROWS)
        self.assertEqual(len(self.busy_state(5).snapshot(rows=50)["rows"]), 5)

    def test_a_token_the_bot_holds_is_never_pushed_off_the_list(self):
        st = TerminalState(FAST)
        st.set_running(True)
        for ev in winner_events("HELD", 1000)[:3]:  # bought into, still open
            st.emit(ev)
        for i in range(40):
            st.emit(Sim(f"LATER{i:02d}", f"C{i:02d}", 5000 + i * 10).create())
        snap = st.snapshot(rows=5)
        mints = [r["mint"] for r in snap["rows"]]
        self.assertEqual(snap["paper"]["open"], 1)
        self.assertEqual(mints, ["LATER39", "LATER38", "LATER37", "LATER36", "LATER35", "HELD"])
        self.assertEqual(snap["rows"][-1]["status"], "holding")

    def test_a_buy_still_filling_is_kept_too(self):
        st = TerminalState(replace(FAST, latency_ms=60_000))
        st.set_running(True)
        for ev in winner_events("FILLING", 1000)[:2]:  # decided to buy, order not filled yet
            st.emit(ev)
        for i in range(40):
            st.emit(Sim(f"LATER{i:02d}", f"C{i:02d}", 5000 + i * 10).create())
        rows = st.snapshot(rows=5)["rows"]
        self.assertEqual([(r["mint"], r["status"]) for r in rows][-1], ("FILLING", "entering"))
        self.assertEqual(len(rows), 6)

    def test_server_passes_the_limit_through(self):
        st = self.busy_state(60)
        srv = make_server(st, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def rows(query):
                with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/state{query}", timeout=5) as r:
                    return len(json.load(r)["rows"])
            self.assertEqual([rows(""), rows("?rows=10"), rows("?range=all&rows=40"), rows("?rows=junk"), rows("?rows=0")], [DEFAULT_ROWS, 10, 40, DEFAULT_ROWS, 1])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_page_offers_the_choice_and_sends_it(self):
        src = INDEX.read_text()
        self.assertIn('id="rowlimit"', src)
        self.assertIn('"&rows=" + $("rowlimit").value', src)


class Laps(unittest.TestCase):
    """A finite recording must not leave the bot with nothing to scan: it repeats, and says so."""

    def recording(self):
        sim = Sim("REAL1", "DEV1", 1000)
        return [sim.create(), sim.scan(1100), sim.buy(1200, 1.0), sim.sell(1500, 1_000_000.0, "DEV1")]

    def test_a_repeat_lap_is_later_renamed_and_keeps_wallet_identity(self):
        events = self.recording()
        lap2 = repeat_lap(events, 2)
        self.assertEqual(len(lap2), len(events))
        self.assertGreater(lap2[0].t, events[-1].t)  # starts after lap 1 ended
        self.assertEqual({e.mint for e in lap2}, {"REAL1~2"})
        creator, seller = lap2[0].creator, lap2[3].trader
        self.assertEqual((creator, seller), ("DEV1~2", "DEV1~2"))  # the creator's own sale is still the creator's
        self.assertEqual([getattr(e, "v_sol", None) for e in lap2], [getattr(e, "v_sol", None) for e in events])  # same prices
        self.assertEqual(repeat_lap(events, 3)[0].t - lap2[0].t, lap2[0].t - events[0].t)  # laps are evenly spaced
        self.assertEqual(events, self.recording())  # the original is not modified

    def test_looping_feed_keeps_emitting_and_reports_each_lap(self):
        stop, got, laps = threading.Event(), [], []
        feed = ReplayFeed(self.recording(), sleep=lambda s: None, loop=True)
        feed.on_lap = laps.append
        def emit(ev):
            got.append(ev)
            if len(got) >= 10:
                stop.set()
        feed.run(emit, stop)
        self.assertEqual(laps, [2, 3])
        self.assertEqual([e.mint for e in got], ["REAL1"] * 4 + ["REAL1~2"] * 4 + ["REAL1~3"] * 2)

    def test_without_loop_the_feed_ends_and_the_state_says_finished(self):
        st = TerminalState(FAST)
        feed = ReplayFeed(self.recording(), sleep=lambda s: None)
        start_feed(feed, st, threading.Event()).join(2)
        self.assertEqual((st.snapshot()["feed_status"], st.snapshot()["lap"]), ("finished", 1))

    def test_state_reports_the_lap_and_logs_it(self):
        st = TerminalState(FAST)
        feed = ReplayFeed(self.recording(), sleep=lambda s: None, loop=True)
        stop = threading.Event()
        count = []
        def emit(ev):
            st.emit(ev)
            count.append(ev)
            if len(count) >= 9:
                stop.set()
        feed.on_lap = st.set_lap
        feed.run(emit, stop)
        snap = st.snapshot()
        self.assertEqual(snap["lap"], 3)
        self.assertTrue(any("lap 3" in l["text"] and "not new evidence" in l["text"] for l in snap["logs"]))

    def test_start_feed_connects_the_lap_counter_to_the_state(self):
        import time
        st, stop = TerminalState(FAST), threading.Event()
        thread = start_feed(ReplayFeed(self.recording(), sleep=lambda s: time.sleep(0.001), loop=True), st, stop)
        deadline = time.time() + 5
        while st.snapshot()["lap"] < 2 and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(2)
        self.assertGreaterEqual(st.snapshot()["lap"], 2)

    def test_page_labels_repeat_laps(self):
        src = INDEX.read_text()
        self.assertIn("REPEAT", src)
        self.assertIn("NOT new evidence", src)


class TunerInTheTerminal(unittest.TestCase):
    """The auto-tuner as the terminal drives it: switched on by the user, run only while the bot trades, applied through the settings rules."""

    def state(self, **cfg):
        from .test_tuner import QUICK
        st = TerminalState(replace(QUICK, take_profit_pct=60, size_caution_sol=0.1, max_open=20), log_size=5000)
        st.configure_tuner({"enabled": True, "clones": 4, "round_launches": 20, "params": ["take_profit"], **cfg})
        return st

    def feed_market(self, st, n=100):
        from .test_tuner import pump_and_dump
        for ev in pump_and_dump(n):
            st.emit(ev)

    def test_it_is_off_until_the_user_switches_it_on(self):
        from .test_tuner import pump_and_dump
        st = TerminalState(FAST)
        st.set_running(True)
        for ev in pump_and_dump(40):
            st.emit(ev)
        t = st.snapshot()["tuner"]
        self.assertEqual((t["enabled"], t["round"], t["clones"], t["history"]), (False, 0, [], []))

    def test_it_does_nothing_while_the_bot_is_stopped(self):
        st = self.state()
        self.feed_market(st, 60)
        t = st.snapshot()["tuner"]
        self.assertEqual((t["enabled"], t["round"], t["history"]), (True, 0, []))

    def test_a_finished_round_changes_the_real_settings_through_the_normal_rules(self):
        st = self.state()
        st.set_running(True)
        self.feed_market(st, 100)
        snap = st.snapshot()
        changed = [h for h in snap["tuner"]["history"] if h["decision"] == "changed"]
        self.assertTrue(changed)
        self.assertLess(snap["settings"]["take_profit"], 60)
        self.assertEqual(st.strategy.take_profit_pct, snap["settings"]["take_profit"])
        self.assertEqual(st.engine.s.take_profit_pct, st.strategy.take_profit_pct)  # the live bot really uses it
        logs = [l for l in st.logs if l["kind"] == "tune"]  # (a snapshot only carries the newest 200 lines)
        self.assertTrue(logs)
        self.assertIn("auto-tune round", logs[0]["text"])
        self.assertTrue(any("Take profit (%)" in l["text"] and " to " in l["text"] for l in logs))
        self.assertTrue(any("settings kept" in l["text"] for l in logs) or len(changed) == len(snap["tuner"]["history"]))

    def test_stopping_the_bot_drops_the_clones_and_starting_begins_a_new_round(self):
        st = self.state()
        st.set_running(True)
        self.feed_market(st, 8)
        self.assertTrue(st.snapshot()["tuner"]["clones"])
        st.set_running(False)
        self.assertEqual(st.snapshot()["tuner"]["clones"], [])
        st.set_running(True)
        self.assertEqual(st.snapshot()["tuner"]["progress"], 0)

    def test_editing_a_setting_by_hand_restarts_the_round_around_the_new_value(self):
        st = self.state()
        st.set_running(True)
        self.feed_market(st, 8)
        self.assertTrue(st.snapshot()["tuner"]["clones"])
        st.update_settings({"take_profit": 45})
        self.assertEqual(st.snapshot()["tuner"]["clones"], [])
        from .test_tuner import pump_and_dump
        for ev in pump_and_dump(5, gap=10_000)[:12]:
            st.emit(replace(ev, t=ev.t + 10_000_000))
        centres = {c["changes"]["take_profit"] for c in st.snapshot()["tuner"]["clones"] if c["changes"]}
        self.assertTrue(centres)
        self.assertTrue(all(30 < v < 60 and v != 45 for v in centres), centres)

    def test_a_repeat_lap_pauses_it_and_the_first_lap_resumes_it(self):
        st = self.state()
        st.set_running(True)
        self.feed_market(st, 8)
        st.set_lap(2)
        t = st.snapshot()["tuner"]
        self.assertIn("already seen", t["paused"])
        self.assertEqual(t["clones"], [])
        self.feed_market(st, 8)
        self.assertEqual(st.snapshot()["tuner"]["clones"], [])
        st.set_lap(1)
        self.assertIsNone(st.snapshot()["tuner"]["paused"])

    def test_resetting_the_paper_account_drops_the_clones(self):
        st = self.state()
        st.set_running(True)
        self.feed_market(st, 8)
        st.reset()
        self.assertEqual(st.snapshot()["tuner"]["clones"], [])

    def test_bad_settings_are_refused_and_nothing_changes(self):
        st = self.state()
        before = st.snapshot()["tuner"]["config"]
        for bad in ({"clones": 99}, {"caution": "reckless"}, {"blend": 5}, {"params": ["not_a_setting"]}, {"enabled": 1}, {"nope": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    st.configure_tuner(bad)
        self.assertEqual(st.snapshot()["tuner"]["config"], before)

    def test_every_value_the_tuner_picks_is_accepted_by_the_settings_screen(self):
        st = self.state(params=[p for p in ("take_profit", "stop_loss", "trailing_stop_pct", "max_hold_s", "min_liquidity_sol", "max_dev_buy_pct", "min_trades")], caution="eager", round_launches=20)
        st.set_running(True)
        market = synth.generate(21, 260, 1_000_000)
        for ev in market:
            st.emit(ev)
        self.assertGreaterEqual(len(st.snapshot()["tuner"]["history"]), 10)
        parse_settings(settings_dict(st.strategy), default_strategy())  # still a valid configuration
