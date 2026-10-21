import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_xrpl_standalone", ROOT / "tools" / "build_xrpl_standalone.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class StandaloneBuild(unittest.TestCase):
    def test_committed_file_is_up_to_date(self):
        self.assertEqual((ROOT / "dist" / "xrpl-cycle-watch.html").read_text(), build.build(),
                         "run: python tools/build_xrpl_standalone.py")

    def test_the_file_runs_by_itself(self):
        html = build.build()
        self.assertNotIn(build.MARKER, html)
        self.assertIn("window.XrplEngine = api", html.replace("root.XrplEngine = api", "window.XrplEngine = api"))
        self.assertRegex(html, r"window\.__PAIRS__ = \[\[")
        self.assertNotRegex(html, r"<script[^>]+src=")  # nothing is fetched from anywhere but the ledger's own websocket
        self.assertNotRegex(html, r"<link[^>]+href=")

    def test_it_only_connects_to_the_xrp_ledger_servers(self):
        engine = (ROOT / "xrpl_arb" / "static" / "engine.js").read_text()
        urls = set(re.findall(r"wss?://[A-Za-z0-9./:_-]+", engine))
        self.assertEqual(urls, {"wss://xrplcluster.com/", "wss://s1.ripple.com/"})
        page = (ROOT / "xrpl_arb" / "static" / "dashboard.html").read_text()
        self.assertNotIn("XMLHttpRequest", page)
        self.assertEqual(re.findall(r"fetch\(([^,)]*)", page), ['"/api/state"'])  # only the local server's data, and only when there is one

    def test_it_never_sends_anything_but_requests_for_data(self):
        engine = (ROOT / "xrpl_arb" / "static" / "engine.js").read_text()
        commands = set(re.findall(r'(?:request|pinned)\("([a-z_]+)"', engine))
        self.assertEqual(commands, {"account_info", "ledger", "subscribe", "amm_info", "book_offers"})  # all read-only
        for word in ("submit", "sign(", "secret", "seed", "private"):
            self.assertNotIn(word, engine.lower(), word)

    def test_the_watched_pairs_are_the_committed_config(self):
        import json
        committed = json.loads((ROOT / "xrpl_arb" / "pairs.mainnet.json").read_text())["pairs"]
        embedded = json.loads(re.search(r"window\.__PAIRS__ = (\[\[.*?\]\]);", build.build()).group(1))
        self.assertEqual(len(embedded), len(committed))
        self.assertEqual(embedded[0], [str(__import__("xrpl_arb.model", fromlist=["Asset"]).Asset.parse(x)) for x in committed[0]])


if __name__ == "__main__":
    unittest.main()
