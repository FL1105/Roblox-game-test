"""Absolute locations, so a run from any directory uses the same ledger and caches (a second ledger would silently undercount trials)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = str(ROOT / "research" / "ledger")
CACHE_DIR = str(ROOT / "research" / "cache")
FROZEN_DIR = str(ROOT / "research" / "frozen")
HOLDOUT_LOG = str(ROOT / "research" / "holdout_log.jsonl")
DEV_DATA = [str(ROOT / "data" / "raw_main.jsonl")]
