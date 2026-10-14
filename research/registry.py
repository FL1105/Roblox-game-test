"""Feature sets are registered by name; a set is a function Context -> {feature name: float}."""

from __future__ import annotations

import glob
import hashlib
import importlib.util
import os
from typing import Callable

from .sandbox import check_source

FEATURE_SETS: dict[str, Callable] = {}
SOURCE_FILES: dict[str, str] = {}   # set name -> file that defined it (hashed into every trial that uses it)


def feature_set(name: str):
    def register(fn: Callable) -> Callable:
        if name in FEATURE_SETS and SOURCE_FILES.get(name) != fn.__code__.co_filename:
            raise ValueError(f"feature set '{name}' is already defined in {SOURCE_FILES[name]}")
        FEATURE_SETS[name] = fn
        SOURCE_FILES[name] = fn.__code__.co_filename
        return fn
    return register


def load_plugins(directory: str = os.path.join(os.path.dirname(__file__), "plugins")) -> list[str]:
    """Import every research/plugins/*.py (each registers its feature sets). Returns the names loaded."""
    loaded = []
    for path in sorted(glob.glob(os.path.join(directory, "*.py"))):
        if os.path.basename(path).startswith("_"):
            continue
        with open(path) as f:
            check_source(f.read(), path)
        mod_name = "research_plugin_" + os.path.basename(path)[:-3]
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        loaded.append(os.path.basename(path))
    return loaded


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]
