"""The Auto-tune screen: static checks on the page, and the /api/tuner contract it depends on, over real HTTP.

The browser behaviour itself is covered by e2e/tuner_ui_test.js (it needs a real browser)."""

import http.client
import json
import re
import threading
import unittest
import urllib.error
import urllib.request

from sniper.paper.engine import Strategy
from sniper.paper.tuner import Tuner
from sniper.terminal.server import INDEX, make_server
from sniper.terminal.state import TerminalState

from .sim import Sim

FAST = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1)
CAUTIONS = {"eager": 80, "balanced": 90, "careful": 95, "strict": 99}
SNAPSHOT_KEYS = {"enabled", "paused", "config", "cautions", "round", "progress", "params", "clones", "bot_mean", "bot_trades", "history"}
CONFIG_KEYS = {"clones", "round_launches", "caution", "blend", "params"}


def launch(i, t0=1000):
    """One scanned, bought-into and pumped launch with its own mint, ten seconds after the previous one."""
    t = t0 + i * 10_000
    sim = Sim(f"M{i:04d}", f"C{i:04d}", t)
    return [sim.create(), sim.scan(t + 100), sim.buy(t + 200, 1.0), sim.buy(t + 300, 12.0), sim.buy(t + 400, 0.5)]


class Page(unittest.TestCase):
    src = INDEX.read_text()

    def test_controls_have_the_exact_accessible_names(self):
        for label in ("Auto-tune switch", "Tuner clones", "Tuner launches per round", "Tuner caution", "Tuner step size"):
            self.assertIn(f'aria-label="{label}"', self.src, label)
        # one checkbox per tunable setting, named after it
        self.assertRegex(self.src, r"setAttribute\(\"aria-label\", `Tune \$\{p\.label\}`\)")

    def test_the_view_nav_item_and_sections_exist(self):
        for needle in ('id="view-tune"', 'data-view="tune"', 'id="nav-tune"', 'id="tuneon"', 'id="tuneparams"', 'id="tunehist"', 'id="roundbar"', 'id="clonerows"', "No rounds finished yet"):
            self.assertIn(needle, self.src, needle)
        self.assertIn("tune:'<path", self.src, "icon missing from IC")
        # without a tuner in /api/state the nav item must be hidden from the start
        self.assertIn('<button data-view="tune" id="nav-tune" hidden>', self.src)

    def test_master_switch_is_a_real_button_with_switch_semantics(self):
        tag = re.search(r"<button[^>]*aria-label=\"Auto-tune switch\"[^>]*>", self.src).group(0)
        self.assertIn('role="switch"', tag)
        self.assertIn("aria-checked=", tag)
        self.assertIn('type="button"', tag)

    def test_inputs_carry_the_documented_limits(self):
        for label, lo, hi in (("Tuner clones", 2, 8), ("Tuner launches per round", 20, 1000), ("Tuner step size", 0.1, 1)):
            tag = re.search(r"<input[^>]*aria-label=\"%s\"[^>]*>" % label, self.src).group(0)
            self.assertIn('type="number"', tag)
            self.assertIn(f'min="{lo}"', tag, label)
            self.assertIn(f'max="{hi}"', tag, label)
        self.assertIn("how far the bot moves toward the winning clones; 0.5 = halfway", self.src.lower())

    def test_every_control_has_a_visible_label_whose_text_is_part_of_its_accessible_name(self):
        for label, field in (("Tuner clones", "tune_clones"), ("Tuner launches per round", "tune_round"), ("Tuner caution", "tune_caution"), ("Tuner step size", "tune_blend")):
            text = re.search(r'<label class="t" for="%s">([^<]+)</label>' % field, self.src).group(1)
            self.assertIn(text.lower(), label.lower())

    def test_limits_are_stated_in_plain_english(self):
        low = self.src.lower()
        for phrase in ("cannot create an edge", "made-up market", "pauses", "only be memorising"):
            self.assertIn(phrase, low, phrase)
        self.assertIn("Start the bot", self.src)

    def test_innerhtml_is_only_used_for_static_icons(self):
        uses = re.findall(r"^.*innerHTML.*$", self.src, re.M)
        self.assertTrue(uses)
        for line in uses:
            self.assertTrue("svg(" in line or "<svg" in line, f"untrusted innerHTML? {line.strip()}")
        self.assertNotIn("insertAdjacentHTML", self.src)
        self.assertNotIn("document.write", self.src)

    def test_no_browser_pop_ups(self):
        # they are blocked in some viewers; the page builds its own two-step confirms and toasts
        self.assertIsNone(re.search(r"(?<![\w.])(confirm|alert|prompt)\s*\(", self.src))
        self.assertNotIn("window.confirm", self.src)
        self.assertNotIn("window.alert", self.src)

    def test_nothing_is_loaded_from_outside(self):
        self.assertIsNone(re.search(r"<script[^>]+src=", self.src))
        self.assertIsNone(re.search(r"<link[^>]+href=[\"']?(https?:)?//", self.src))
        self.assertNotIn("@import", self.src)
        self.assertIsNone(re.search(r"url\(\s*[\"']?https?:", self.src))
        self.assertIsNone(re.search(r"\b(XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b", self.src))

    def test_dynamic_text_goes_in_through_textcontent(self):
        """Everything the tune section builds comes from el()/textContent; none of the tuner code touches innerHTML."""
        start = self.src.index("/* ---- Auto-tune")
        end = self.src.index("function render(s) {")
        self.assertNotIn("innerHTML", self.src[start:end])
        self.assertNotIn("outerHTML", self.src[start:end])

    def test_the_page_tolerates_a_server_without_a_tuner(self):
        # render() hands every snapshot to renderTuneAll, which hides the nav item when `tuner` is missing and leaves the view if it was open
        self.assertRegex(self.src, r'\$\("nav-tune"\)\.hidden = !t;')
        self.assertRegex(self.src, r'if \(!t\) \{ if \(view === "tune"\) show\("dashboard"\); return; \}')
        self.assertIn("renderTuneAll(s, false);", self.src)

    def test_the_page_only_sends_keys_the_server_knows(self):
        sent = set(re.findall(r"\[\"(clones|round_launches|caution|blend)\", \"tune_", self.src)) | set(re.findall(r"tunePost\(\{(enabled|params)\b", self.src))
        self.assertEqual(sent, {"clones", "round_launches", "caution", "blend", "enabled", "params"})


class Contract(unittest.TestCase):
    """What the page relies on, against the real server."""

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

    def setUp(self):  # every test starts from the defaults on a fresh account
        self.state.tuner = Tuner()
        self.state.set_running(False)
        self.state.set_lap(1)
        self.state.reset()

    def req(self, path, body=None, headers=None, raw=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def tuner(self):
        code, body = self.req("/api/state")
        self.assertEqual(code, 200)
        return body["tuner"]

    def post(self, body):
        return self.req("/api/tuner", body)

    def test_state_carries_a_complete_tuner_snapshot(self):
        t = self.tuner()
        self.assertEqual(set(t), SNAPSHOT_KEYS)
        self.assertEqual(set(t["config"]), CONFIG_KEYS)
        self.assertEqual(t["cautions"], CAUTIONS)
        self.assertFalse(t["enabled"])
        self.assertIsNone(t["paused"])
        c = t["config"]
        self.assertEqual((c["clones"], c["round_launches"], c["caution"], c["blend"]), (6, 100, "balanced", 0.5))
        self.assertEqual((t["round"], t["progress"], t["clones"], t["history"], t["bot_trades"]), (0, 0, [], [], 0))
        self.assertTrue(t["params"])
        keys = [p["key"] for p in t["params"]]
        self.assertEqual(len(keys), len(set(keys)), "parameter keys are unique")
        for p in t["params"]:
            self.assertEqual(set(p), {"key", "label", "tuned", "active", "lo", "hi", "value"})
            self.assertIsInstance(p["label"], str)
            self.assertLessEqual(p["lo"], p["hi"])
            self.assertIn(p["key"], c["params"])  # everything starts out tunable
            self.assertTrue(p["tuned"])

    def test_valid_posts_come_back_as_the_new_snapshot_and_show_in_state(self):
        code, body = self.post({"enabled": True})
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {"tuner"})
        self.assertEqual(set(body["tuner"]), SNAPSHOT_KEYS)
        self.assertTrue(body["tuner"]["enabled"])
        self.assertTrue(self.tuner()["enabled"])

        code, body = self.post({"clones": 4, "round_launches": 20, "caution": "careful", "blend": 0.3})
        self.assertEqual(code, 200)
        self.assertEqual({k: v for k, v in self.tuner()["config"].items() if k != "params"}, {"clones": 4, "round_launches": 20, "caution": "careful", "blend": 0.3})
        self.assertEqual(body["tuner"]["config"], self.tuner()["config"])
        self.assertTrue(self.tuner()["enabled"], "a partial update must not switch the tuner off")

        code, body = self.post({"enabled": False})
        self.assertEqual(code, 200)
        self.assertFalse(self.tuner()["enabled"])
        self.assertEqual(self.tuner()["config"]["clones"], 4)

    def test_parameter_list_round_trips_and_is_reflected_per_parameter(self):
        keep = ["take_profit", "stop_loss"]
        self.assertEqual(self.post({"params": keep})[0], 200)
        t = self.tuner()
        self.assertEqual(t["config"]["params"], keep)
        self.assertEqual({p["key"] for p in t["params"] if p["tuned"]}, set(keep))
        self.assertEqual(self.post({"params": []})[0], 200)  # nothing tunable is allowed: the clones then just copy the bot
        t = self.tuner()
        self.assertEqual(t["config"]["params"], [])
        self.assertFalse(any(p["tuned"] for p in t["params"]))

    def test_every_offered_setting_can_be_selected_on_its_own(self):
        for k in [p["key"] for p in self.tuner()["params"]]:
            self.assertEqual(self.post({"params": [k]})[0], 200, k)
            self.assertEqual(self.tuner()["config"]["params"], [k])

    def test_boundary_values_are_accepted(self):
        for payload in ({"clones": 2}, {"clones": 8}, {"round_launches": 20}, {"round_launches": 1000}, {"blend": 0.1}, {"blend": 1}, {"blend": 1.0}, {"clones": 3.0}):
            code, body = self.post(payload)
            self.assertEqual(code, 200, f"{payload} {body}")
        for name in CAUTIONS:
            self.assertEqual(self.post({"caution": name})[0], 200, name)
            self.assertEqual(self.tuner()["config"]["caution"], name)

    def test_invalid_payloads_get_a_400_with_a_message_and_change_nothing(self):
        self.post({"clones": 5, "round_launches": 50, "caution": "strict", "blend": 0.7, "enabled": True})
        before = self.tuner()
        bad = [
            {"clones": 99}, {"clones": 1}, {"clones": 0}, {"clones": -3}, {"clones": 9}, {"clones": 2.5}, {"clones": "4"}, {"clones": True}, {"clones": None}, {"clones": [4]},
            {"clones": float("nan")}, {"clones": float("inf")},
            {"round_launches": 19}, {"round_launches": 1001}, {"round_launches": 5000}, {"round_launches": 20.5}, {"round_launches": "20"}, {"round_launches": False},
            {"caution": "reckless"}, {"caution": ""}, {"caution": 90}, {"caution": None}, {"caution": "Balanced"},
            {"blend": 0}, {"blend": 0.09}, {"blend": 1.01}, {"blend": 5}, {"blend": "0.5"}, {"blend": True}, {"blend": None}, {"blend": float("nan")}, {"blend": [1]},
            {"enabled": "yes"}, {"enabled": 1}, {"enabled": 0}, {"enabled": None}, {"enabled": "true"}, {"enabled": [True]},
            {"params": ["nope"]}, {"params": "take_profit"}, {"params": ["take_profit", "take_profit"]}, {"params": [1]}, {"params": None}, {"params": {"take_profit": True}}, {"params": ["take_profit", "nope"]},
            {"nonsense": 1}, {"clones": 4, "nonsense": 1}, {"clones": 4, "round_launches": 5},  # one bad field spoils the whole update
        ]
        for payload in bad:
            code, body = self.post(payload)
            self.assertEqual(code, 400, str(payload))
            self.assertIsInstance(body.get("error"), str, str(payload))
            self.assertTrue(body["error"].strip(), str(payload))
            self.assertNotIn("Traceback", body["error"])
        self.assertEqual(before, self.tuner())

    def test_unhashable_values_are_a_400_not_a_dropped_connection(self):
        # a JSON list/object where a name is expected must be refused like any other bad value
        before = self.tuner()
        for payload in ({"caution": ["balanced"]}, {"caution": {"a": 1}}, {"params": [["take_profit"]]}, {"params": [{"k": 1}]}):
            try:
                code, body = self.post(payload)
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError) as e:
                self.fail(f"{payload}: the server dropped the connection instead of answering 400 ({type(e).__name__}); parse_tuner raises TypeError, not ValueError")
            self.assertEqual(code, 400, str(payload))
            self.assertTrue(body["error"].strip())
        self.assertEqual(before, self.tuner())

    def test_error_messages_name_the_field_in_words_a_person_can_use(self):
        cases = [({"clones": 99}, "clones"), ({"round_launches": 5}, "launches"), ({"blend": 3}, "step"), ({"caution": "x"}, "caution"), ({"enabled": "yes"}, "auto-tune"), ({"nonsense": 1}, "nonsense")]
        for payload, word in cases:
            code, body = self.post(payload)
            self.assertEqual(code, 400)
            self.assertIn(word, body["error"].lower(), str(payload))

    def test_malformed_bodies_and_foreign_hosts_are_refused(self):
        self.assertEqual(self.req("/api/tuner", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("/api/tuner", raw=b"[1]")[0], 400)
        self.assertEqual(self.req("/api/tuner", raw=b"enabled=true", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("/api/tuner", {"enabled": True}, headers={"Host": "evil.example.com"})[0], 403)
        self.assertFalse(self.tuner()["enabled"])

    def test_an_empty_post_changes_nothing(self):
        before = self.tuner()["config"]
        code, body = self.post({})
        self.assertEqual(code, 200)
        self.assertEqual(body["tuner"]["config"], before)

    def test_a_running_round_shows_progress_clones_and_then_a_history_entry_and_a_log_line(self):
        self.post({"enabled": True, "clones": 4, "round_launches": 20})
        self.state.set_running(True)
        for i in range(6):  # the first launch only starts the round; the next five are counted
            for ev in launch(i):
                self.state.emit(ev)
        t = self.tuner()
        self.assertEqual((t["round"], t["progress"], len(t["clones"])), (1, 5, 4))
        self.assertEqual([c["id"] for c in t["clones"]], [1, 2, 3, 4])
        keys = {p["key"] for p in t["params"]}
        for c in t["clones"]:
            self.assertEqual(set(c), {"id", "changes", "trades", "mean", "delta"})
            self.assertTrue(set(c["changes"]) <= keys)
            self.assertTrue(all(isinstance(v, (int, float)) for v in c["changes"].values()))
        for i in range(6, 30):
            for ev in launch(i):
                self.state.emit(ev)
        code, snap = self.req("/api/state")
        t = snap["tuner"]
        self.assertGreaterEqual(len(t["history"]), 1)
        h = t["history"][-1]  # newest first, so the oldest entry is the first round
        self.assertEqual({"round", "t", "launches", "bot_mean", "bot_trades", "decision", "reason", "changes", "clones"}, set(h))
        self.assertEqual(h["round"], 1)
        self.assertIn(h["decision"], ("changed", "kept"))
        self.assertTrue(h["reason"])
        self.assertEqual(h["launches"], 20)
        self.assertEqual(len(h["clones"]), 4)
        for c in h["clones"]:
            self.assertEqual({"id", "changes", "trades", "mean", "lcb", "differing", "passed"}, set(c))
            self.assertIsInstance(c["passed"], bool)
        if h["decision"] == "changed":
            self.assertTrue(h["changes"])
            for k, (old, new) in h["changes"].items():
                self.assertIn(k, keys)
                self.assertNotEqual(old, new)
        else:
            self.assertEqual(h["changes"], {})
        lines = [l for l in snap["logs"] if l["kind"] == "tune"]
        self.assertEqual(len(lines), len(t["history"]))
        self.assertIn("round 1", lines[-1]["text"])

    def test_a_stopped_bot_runs_no_clones(self):
        self.post({"enabled": True, "round_launches": 20})
        for i in range(5):
            for ev in launch(i):
                self.state.emit(ev)
        t = self.tuner()
        self.assertTrue(t["enabled"])
        self.assertEqual((t["clones"], t["progress"], t["round"]), ([], 0, 0))

    def test_repeat_laps_pause_the_tuner_with_a_reason_the_page_can_show(self):
        self.state.set_lap(2)
        try:
            t = self.tuner()
            self.assertIsInstance(t["paused"], str)
            self.assertIn("repeat", t["paused"])
        finally:
            self.state.set_lap(1)
        self.assertIsNone(self.tuner()["paused"])

    def test_changing_the_tuner_restarts_the_round(self):
        self.post({"enabled": True, "clones": 4, "round_launches": 20})
        self.state.set_running(True)
        for i in range(4):
            for ev in launch(i):
                self.state.emit(ev)
        self.assertGreater(self.tuner()["progress"], 0)
        self.post({"clones": 5})
        t = self.tuner()
        self.assertEqual((t["progress"], t["clones"]), (0, []))


if __name__ == "__main__":
    unittest.main()
