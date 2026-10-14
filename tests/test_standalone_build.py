import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_standalone", ROOT / "tools" / "build_standalone.py")
build_standalone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_standalone)


class StandaloneBuild(unittest.TestCase):
    def test_committed_files_are_up_to_date(self):
        full, frag = build_standalone.build()
        self.assertEqual((ROOT / "dist" / "launch-sniper.html").read_text(), full, "run: python tools/build_standalone.py")
        self.assertEqual((ROOT / "dist" / "launch-sniper.fragment.html").read_text(), frag, "run: python tools/build_standalone.py")

    def test_fragment_has_no_document_wrapper_and_one_title(self):
        _, frag = build_standalone.build()
        self.assertEqual(re.findall(r"<(?:!doctype|html|head|body)[\s>]", frag, re.I), [])
        self.assertEqual(len(re.findall(r"<title>", frag)), 1)
        self.assertIn("<title>Launch Sniper Paper Terminal</title>", frag)

    def test_browser_code_never_touches_the_network_or_a_wallet(self):
        js = (ROOT / "sniper/terminal/static/standalone.js").read_text()
        urls = set(re.findall(r"https?://[^\s\"'`)]+", js))
        self.assertLessEqual(urls, {"http://local"})
        for word in ("WebSocket", "XMLHttpRequest", "sendBeacon", "EventSource", "window.solana", "signTransaction", "privateKey", "eval("):
            self.assertNotIn(word, js)

    def test_page_leaves_data_theme_to_the_host(self):
        page = (ROOT / "sniper/terminal/static/index.html").read_text()
        self.assertNotIn("dataset.theme", page)
        self.assertNotIn("confirm(", page)  # browser pop-ups are unavailable in some viewers


if __name__ == "__main__":
    unittest.main()
