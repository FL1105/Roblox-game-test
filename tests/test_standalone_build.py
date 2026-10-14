import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_standalone", ROOT / "tools" / "build_standalone.py")
build_standalone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_standalone)


class StandaloneBuild(unittest.TestCase):
    BLOCK = re.compile(r'<script type="application/json" id="sniper-real">.*?</script>\n', re.S)

    def test_committed_files_are_up_to_date_apart_from_the_embedded_data(self):
        full, frag = build_standalone.build()
        for name, expected in (("launch-sniper.html", full), ("launch-sniper.fragment.html", frag)):
            committed = self.BLOCK.sub("", (ROOT / "dist" / name).read_text())
            self.assertEqual(committed, expected, "run: python tools/build_standalone.py [--events ...]")

    def test_hostile_token_names_cannot_break_out_of_the_data_block(self):
        import json
        import tempfile
        from sniper.pump.events import NewToken, write_jsonl
        evil = "</script><img src=x onerror=alert(1)><script>window.__xss=1</script>\u2028"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "e.jsonl"
            write_jsonl(str(path), [NewToken(1, "M", "C", evil, evil, 0.0, 0.0, 30.0, 1.073e9)])
            full, frag = build_standalone.build(str(path), None, "label </script>")
        for text in (full, frag):
            block = self.BLOCK.search(text)
            self.assertIsNotNone(block)
            inner = block.group(0)[len('<script type="application/json" id="sniper-real">'):-len("</script>\n")]
            self.assertNotIn("<", inner)
            self.assertNotIn(">", inner)
            self.assertNotIn("\u2028", inner)
            self.assertEqual(json.loads(inner)["events"][0]["name"], evil)  # still decodes to the exact original text
            self.assertEqual(text.count('id="sniper-real"'), 1)
            self.assertNotIn("<img src=x", text)

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
