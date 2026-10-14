"""Feature sets are registered by name; a set is a function Context -> {feature name: float}."""

from __future__ import annotations

import glob
import hashlib
import importlib.util
import os
from typing import Callable

from .sandbox import check_source

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "plugins")
FEATURE_SETS: dict[str, Callable] = {}
SOURCE_FILES: dict[str, str] = {}   # set name -> file that defined it
SOURCE_HASH: dict[str, str] = {}    # set name -> hash of that file as it was when it was loaded (what actually runs), hashed into every cache and trial
PLUGIN_ERRORS: dict[str, str] = {}  # plugin file -> why it was rejected or failed to load (a broken file never takes the others down)


def feature_set(name: str):
    def register(fn: Callable) -> Callable:
        if name in FEATURE_SETS and SOURCE_FILES.get(name) != fn.__code__.co_filename:
            raise ValueError(f"feature set '{name}' is already defined in {SOURCE_FILES[name]}")
        FEATURE_SETS[name] = fn
        SOURCE_FILES[name] = fn.__code__.co_filename
        try:
            SOURCE_HASH[name] = file_hash(fn.__code__.co_filename)
        except OSError:
            SOURCE_HASH[name] = "unknown"
        return fn
    return register


def load_plugins(directory: str = PLUGIN_DIR, strict: bool = False) -> list[str]:
    """Import every plugin file in `directory` (each registers its feature sets). A rejected or broken file is recorded in PLUGIN_ERRORS and skipped
    (or raised, with strict=True); returns the files loaded."""
    loaded = []
    for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
        base = os.path.basename(path)
        if base.startswith("_"):
            continue
        try:
            with open(path) as f:
                check_source(f.read(), path)
            spec = importlib.util.spec_from_file_location("research_plugin_" + base[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001  (a half-written or rejected plugin must not break everyone else)
            PLUGIN_ERRORS[base] = f"{type(e).__name__}: {e}"
            if strict:
                raise
            continue
        loaded.append(base)
    return loaded


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]
