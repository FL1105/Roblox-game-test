"""Builds the no-install browser version of the terminal from the same page the Python server serves.

    python tools/build_standalone.py          # writes dist/launch-sniper.html (double-click it)
                                              #        dist/launch-sniper.fragment.html (for hosts that add their own <html>)
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sniper" / "terminal" / "static"
MARKER = "<!--standalone-shim-->"


def build() -> tuple[str, str]:
    page = (STATIC / "index.html").read_text()
    shim = (STATIC / "standalone.js").read_text()
    if MARKER not in page:
        raise SystemExit("index.html lost its standalone marker")
    full = page.replace(MARKER, f"<script>\n{shim}\n</script>", 1)

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
    full, frag = build()
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    (out / "launch-sniper.html").write_text(full)
    (out / "launch-sniper.fragment.html").write_text(frag)
    print(f"wrote {out / 'launch-sniper.html'} ({len(full) // 1024} KB) and the fragment ({len(frag) // 1024} KB)")


if __name__ == "__main__":
    main()
