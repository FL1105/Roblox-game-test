"""Builds the no-install, no-server browser version of the XRPL cycle dashboard.

    python tools/build_xrpl_standalone.py       # writes dist/xrpl-cycle-watch.html (double-click it)

The page is the same one `python -m xrpl_arb ui` serves (xrpl_arb/static/dashboard.html), with the engine
(xrpl_arb/static/engine.js: the Python package's maths and ledger watcher in JavaScript) and the watched pairs baked in,
so the browser reads the XRP Ledger itself over a websocket. Read-only: it asks for data and has no key or wallet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # so `python tools/build_xrpl_standalone.py` can import xrpl_arb
STATIC = ROOT / "xrpl_arb" / "static"
PAIRS = ROOT / "xrpl_arb" / "pairs.mainnet.json"
MARKER = "<!--engine-->"
OUT = ROOT / "dist" / "xrpl-cycle-watch.html"


def build() -> str:
    from xrpl_arb.__main__ import load_config

    page = (STATIC / "dashboard.html").read_text()
    engine = (STATIC / "engine.js").read_text()
    if page.count(MARKER) != 1:
        raise SystemExit(f"dashboard.html must contain {MARKER} exactly once")
    if "</script" in engine.lower():
        raise SystemExit("engine.js must not contain a closing script tag")
    _, pairs = load_config(str(PAIRS))
    data = json.dumps([[str(a), str(b)] for a, b in pairs], separators=(",", ":")).replace("<", "\\u003c")
    block = f"<script>\n{engine}</script>\n<script>window.__PAIRS__ = {data};</script>"
    return page.replace(MARKER, block)


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
