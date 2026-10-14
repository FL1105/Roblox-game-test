"""One hash for everything that determines what a number means. It goes into every cache key, ledger entry and frozen spec,
so a change to the rules can never silently reuse an old result."""

from __future__ import annotations

import glob
import hashlib
import importlib.metadata
import os

from .paths import ROOT

_CODE = [
    "research/*.py",                     # the harness (plugins are hashed per feature set)
    "sniper/pump/*.py", "sniper/policy/*.py", "sniper/paper/engine.py", "sniper/paper/stats.py", "sniper/live/convert.py", "sniper/live/infer.py",
]
_cache: str | None = None


def code_hash(refresh: bool = False) -> str:
    global _cache
    if _cache is None or refresh:
        h = hashlib.sha256()
        for pattern in _CODE:
            for path in sorted(glob.glob(str(ROOT / pattern))):
                h.update(os.path.relpath(path, ROOT).encode())
                with open(path, "rb") as f:
                    h.update(f.read())
        for lib in ("numpy", "scikit-learn", "lightgbm", "scipy"):
            try:
                h.update(f"{lib}={importlib.metadata.version(lib)}".encode())
            except importlib.metadata.PackageNotFoundError:
                h.update(f"{lib}=none".encode())
        _cache = h.hexdigest()[:12]
    return _cache
