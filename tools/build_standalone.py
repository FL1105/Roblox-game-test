"""Builds the no-install browser version of the terminal from the same page the Python server serves.

    python tools/build_standalone.py          # writes dist/launch-sniper.html (double-click it)
                                              #        dist/launch-sniper.fragment.html (for hosts that add their own <html>)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # so `python tools/build_standalone.py` can import the sniper package
STATIC = ROOT / "sniper" / "terminal" / "static"
MARKER = "<!--standalone-shim-->"


def embed_block(events_path: str | None, model_path: str | None, label: str | None) -> str:
    """A JSON data block for the page. `<` is escaped so no token name can close the tag or open another."""
    if not events_path:
        return ""
    from sniper.pump.events import read_jsonl, to_wire

    events = [to_wire(e) for e in read_jsonl(events_path)]
    model = json.loads(Path(model_path).read_text()) if model_path else None
    payload = {"label": label or "Real recorded launches", "events": events, "model": model}
    text = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f'<script type="application/json" id="sniper-real">{text}</script>\n'


def build(events_path: str | None = None, model_path: str | None = None, label: str | None = None) -> tuple[str, str]:
    page = (STATIC / "index.html").read_text()
    shim = (STATIC / "standalone.js").read_text()
    if MARKER not in page:
        raise SystemExit("index.html lost its standalone marker")
    full = page.replace(MARKER, f"{embed_block(events_path, model_path, label)}<script>\n{shim}\n</script>", 1)

    frag = full
    frag = re.sub(r"<!doctype html>\s*", "", frag, flags=re.I)
    frag = re.sub(r"</?html[^>]*>\s*", "", frag)
    frag = re.sub(r"</?head>\s*", "", frag)
    frag = re.sub(r"</?body[^>]*>\s*", "", frag)
    frag = re.sub(r'<meta charset[^>]*>\s*', "", frag)
    frag = re.sub(r'<meta name="viewport"[^>]*>\s*', "", frag)
    frag = re.sub(r'<link rel="icon"[^>]*>\s*', "", frag)
    return full, frag


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the no-install browser version")
    ap.add_argument("--events", help="JSONL of real recorded events to embed for replay")
    ap.add_argument("--model", help="trained hold/sell model JSON to embed")
    ap.add_argument("--label", help="what to call the recorded data in the page")
    a = ap.parse_args()
    full, frag = build(a.events, a.model, a.label)
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    (out / "launch-sniper.html").write_text(full)
    (out / "launch-sniper.fragment.html").write_text(frag)
    print(f"wrote {out / 'launch-sniper.html'} ({len(full) // 1024} KB) and the fragment ({len(frag) // 1024} KB)")


if __name__ == "__main__":
    main()
