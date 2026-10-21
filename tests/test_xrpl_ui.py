import glob
import http.client
import json
import re
import threading
import unittest

from xrpl_arb.model import XRP, Asset
from xrpl_arb.ui import PAGE, DashboardState, make_server
from xrpl_arb.stream import watch

from .test_xrpl_arb import BITSTAMP, EUR, USD, FakeLedger, FakeWS, close_msg, iou, node, settings, tx_msg

CFG = {"min_profit": 0.001, "min_profit_pct": 0.25, "tx_cost": 0.00002, "max_legs": 4, "max_xrp": 1000, "account": None}
OFFER_CHANGED = tx_msg(node("Modified", "Offer", TakerGets=iou(EUR, 45), TakerPays="90000000"))


def run(script, ledger=None, **kw):
    """Run the watcher over a scripted stream; return the dashboard state it built and the rows it recorded."""
    ledger = ledger or FakeLedger(books={(XRP, USD): [(100, 50)], (USD, EUR): [(50, 45)], (EUR, XRP): [(45, 90)]})
    dash, rows = DashboardState(dict(CFG)), []
    sink = type("Sink", (), {"write": lambda self, text: rows.extend(json.loads(l) for l in text.splitlines()),
                             "flush": lambda self: None})()
    pairs = [(XRP, USD), (USD, EUR), (EUR, XRP)]
    watch(ledger, pairs, settings(**kw), connect=lambda url: FakeWS(script(ledger)), log=lambda line: None,
          sink=sink, observer=dash.observe)
    return dash, rows


def open_then_close(ledger):
    set_payout = lambda v: (lambda: ledger.books.__setitem__((EUR, XRP), [(45, v)]))
    return [close_msg(100), set_payout(112.5), OFFER_CHANGED, close_msg(101), close_msg(102),
            set_payout(90), OFFER_CHANGED, close_msg(103)]


class StateFromTheWatcher(unittest.TestCase):
    def test_follows_an_opportunity_and_reports_its_lifetime(self):
        dash, _ = run(open_then_close, max_ledgers=4)
        snap = dash.snapshot()
        self.assertEqual([r["ledger"] for r in snap["ledgers"]], [100, 101, 102, 103])
        self.assertEqual([r["opportunities"] for r in snap["ledgers"]], [0, 1, 1, 0])
        self.assertEqual(snap["live"], [])
        self.assertEqual(len(snap["ended"]), 1)
        ended = snap["ended"][0]
        self.assertEqual((ended["first_ledger"], ended["ledgers_alive"]), (101, 2))
        self.assertEqual(ended["labels"], ["XRP", "USD", "EUR", "XRP"])
        k = snap["kpis"]
        self.assertEqual((k["live"], k["seen"], k["ended"], k["median_lifetime"], k["ledgers"]), (0, 1, 1, 2, 4))
        self.assertAlmostEqual(k["best_seen"]["profit"], 12.5, places=3)
        self.assertEqual(snap["status"]["state"], "connected")
        self.assertEqual(snap["status"]["ledger"], 103)
        texts = [e["text"] for e in snap["log"]]
        self.assertTrue(any(t.startswith("new +12.4") for t in texts) and any("ended after 2 ledger" in t for t in texts))

    def test_a_live_opportunity_carries_its_legs(self):
        dash, _ = run(lambda ledger: open_then_close(ledger)[:5], max_ledgers=3)
        snap = dash.snapshot()
        self.assertEqual((snap["kpis"]["live"], snap["ended"]), (1, []))
        opp = snap["live"][0]
        self.assertEqual((opp["first_ledger"], opp["last_ledger"]), (101, 102))
        self.assertEqual(opp["labels"], ["XRP", "USD", "EUR", "XRP"])
        self.assertEqual([(l["in"], l["out"]) for l in opp["legs"]], [("XRP", "USD"), ("USD", "EUR"), ("EUR", "XRP")])
        self.assertGreater(opp["legs"][-1]["amount_out"], opp["legs"][0]["amount_in"])

    def test_best_cycle_per_ledger_is_recorded_even_when_nothing_clears(self):
        dash, _ = run(open_then_close, max_ledgers=4, min_profit=50)  # the bar is too high: nothing is an opportunity
        rows = dash.snapshot()["ledgers"]
        self.assertEqual([r["opportunities"] for r in rows], [0, 0, 0, 0])
        self.assertIsNone(rows[0]["best_gross_xrp"])  # nothing pays at any size in ledger 100
        self.assertAlmostEqual(rows[1]["best_gross_xrp"], 12.5, places=3)  # the near miss is still charted
        self.assertEqual(rows[1]["best_labels"], ["XRP", "USD", "EUR", "XRP"])

    def test_markets_table_counts_offers_and_remembers_changes(self):
        dash, _ = run(open_then_close, max_ledgers=4)
        by_pair = {(m["a"], m["b"]): m for m in dash.snapshot()["markets"]}
        self.assertEqual(set(by_pair), {("XRP", "USD"), ("USD", "EUR"), ("EUR", "XRP")})
        m = by_pair[("EUR", "XRP")]
        self.assertEqual((m["offers_ab"], m["offers_ba"], m["pool"]), (1, 0, None))
        self.assertEqual(m["changed"], 103)  # the last ledger in which its books differed
        self.assertEqual(by_pair[("XRP", "USD")]["changed"], 100)  # read once, never changed again

    def test_pool_depth_is_reported_in_xrp(self):
        ledger = FakeLedger(pools={frozenset((XRP, USD)): ((XRP, 1000), (USD, 500), 300)})
        dash, _ = run(lambda l: [close_msg(100)], ledger=ledger, max_ledgers=1)
        pool = next(m for m in dash.snapshot()["markets"] if (m["a"], m["b"]) == ("XRP", "USD"))["pool"]
        self.assertEqual((pool["reserve_a"], pool["reserve_b"], pool["fee_pct"], pool["tvl_xrp"]), (1000, 500, 0.3, 2000))
        self.assertEqual(pool["price"], 2.0)  # XRP per USD

    def test_connection_changes_reach_the_status(self):
        dash = DashboardState(dict(CFG))
        dash.observe({"kind": "status", "state": "reconnecting", "detail": "OSError: down"})
        self.assertEqual(dash.snapshot()["status"]["state"], "reconnecting")
        self.assertIn("reconnecting", dash.snapshot()["log"][0]["text"])

    def test_snapshot_is_plain_json(self):
        dash, _ = run(open_then_close, max_ledgers=4)
        json.dumps(dash.snapshot())  # no Decimal or set leaks into what the page fetches


class ReplayFromARecording(unittest.TestCase):
    def test_replay_rebuilds_the_same_story(self):
        live, rows = run(open_then_close, max_ledgers=4)
        replay = DashboardState.from_rows(rows, dict(CFG)).snapshot()
        want = live.snapshot()
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["status"]["state"], "replay")
        self.assertEqual([(r["ledger"], r["opportunities"], r["candidates"]) for r in replay["ledgers"]],
                         [(r["ledger"], r["opportunities"], r["candidates"]) for r in want["ledgers"]])
        self.assertEqual([(e["labels"], e["ledgers_alive"]) for e in replay["ended"]],
                         [(e["labels"], e["ledgers_alive"]) for e in want["ended"]])
        self.assertEqual(replay["kpis"]["seen"], want["kpis"]["seen"])
        self.assertAlmostEqual(replay["ledgers"][1]["best_gross_xrp"], 12.5, places=3)

    def test_an_opportunity_still_open_at_the_end_is_live_in_the_replay(self):
        _, rows = run(lambda ledger: open_then_close(ledger)[:5], max_ledgers=3)
        snap = DashboardState.from_rows(rows, dict(CFG)).snapshot()
        self.assertEqual(len(snap["live"]), 1)
        self.assertEqual(snap["live"][0]["labels"], ["XRP", "USD", "EUR", "XRP"])
        self.assertEqual(len(snap["live"][0]["legs"]), 3)

    def test_old_recordings_without_the_best_gain_field_still_chart(self):
        _, rows = run(open_then_close, max_ledgers=4, min_profit=50)
        for r in rows:
            r.pop("best_gross_xrp", None)  # runs recorded before the field existed
        snap = DashboardState.from_rows(rows, dict(CFG)).snapshot()
        self.assertAlmostEqual(snap["ledgers"][1]["best_gross_xrp"], 12.5, places=3)

    def test_markets_can_be_supplied_for_a_replay(self):
        snap = DashboardState.from_rows([], dict(CFG), markets=[{"a": "XRP", "b": "USD"}]).snapshot()
        self.assertEqual(snap["markets"], [{"a": "XRP", "b": "USD"}])


class Serving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dash, _ = run(open_then_close, max_ledgers=4)
        cls.server = make_server(dash, 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        res = conn.getresponse()
        body = res.read()
        conn.close()
        return res, body

    def test_serves_the_page_with_a_locked_down_policy(self):
        res, body = self.get("/")
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.getheader("Content-Type"))
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("http", csp.replace("http-equiv", ""))  # no outside host is whitelisted
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(body, PAGE.read_bytes())

    def test_serves_the_state_as_json(self):
        res, body = self.get("/api/state")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Content-Type"), "application/json")
        data = json.loads(body)
        self.assertEqual(data["kpis"]["seen"], 1)
        self.assertEqual(len(data["markets"]), 3)

    def test_refuses_a_foreign_host_header(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1:1"):
            res, _ = self.get("/api/state", host=host)
            self.assertEqual(res.status, 403, host)
        res, _ = self.get("/", host=f"localhost:{self.port}")
        self.assertEqual(res.status, 200)

    def test_unknown_paths_and_other_methods(self):
        self.assertEqual(self.get("/nope")[0].status, 404)
        self.assertEqual(self.get("/api/state", method="POST")[0].status, 501)

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class PageSource(unittest.TestCase):
    """The page shows chain data (token names) that anyone can choose, so it must never turn it into markup."""

    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text()

    def test_never_builds_html_from_data(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "srcdoc"):
            self.assertNotIn(banned, self.html, banned)

    def test_makes_no_requests_beyond_its_own_server(self):
        urls = set(re.findall(r"https?://[^\s\"')]+", self.html))
        self.assertEqual(urls, {"http://www.w3.org/2000/svg"})  # the SVG namespace, not a request
        self.assertNotRegex(self.html, r"<script[^>]+src=")
        self.assertNotRegex(self.html, r"<link[^>]+href=")
        self.assertEqual(re.findall(r"fetch\(([^,)]*)", self.html), ['"/api/state"'])

    def test_has_no_trading_controls(self):
        for word in ("private key", "secret", "seed phrase", "mnemonic", "wallet connect", "<form", "<textarea",
                     'type="password"', "submit"):
            self.assertNotIn(word, self.html.lower(), word)

    def test_both_themes_and_a_phone_layout_exist(self):
        for needle in ('prefers-color-scheme: dark', ':root[data-theme="dark"]', 'name="viewport"', "@media (max-width: 960px)"):
            self.assertIn(needle, self.html)


def _chromium():
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else None


@unittest.skipUnless(_chromium(), "needs a Chromium install and `pip install playwright`")
class InTheBrowser(unittest.TestCase):
    def test_a_hostile_token_name_is_shown_as_text_and_runs_nothing(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        evil = "<svg onload=pwned=1>"  # 20 characters: a legal on-chain currency name that would run if it became markup
        self.assertEqual(len(evil), 20)
        asset = Asset.parse(f"{evil}+{BITSTAMP}")
        self.assertEqual(asset.label, evil)
        ledger = FakeLedger(books={(XRP, asset): [(100, 50)], (asset, XRP): [(50, 90)]},
                            pools={frozenset((XRP, asset)): ((XRP, 1000), (asset, 500), 300)})
        dash = DashboardState(dict(CFG))
        watch(ledger, [(XRP, asset)], settings(max_ledgers=1), connect=lambda url: FakeWS([close_msg(100)]),
              log=lambda line: None, observer=dash.observe)
        server = make_server(dash, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])
                page = browser.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
                page.wait_for_selector("#markets tbody tr")
                self.assertIn(evil, page.inner_text("#markets"))  # shown literally
                self.assertEqual(page.evaluate("window.pwned === undefined"), True)
                self.assertEqual(page.locator("#markets svg").count(), 0)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
