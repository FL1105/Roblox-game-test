"""Every trial ever run, appended to a file nobody edits: the count of trials is what makes the final verdict honest."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
import warnings

import numpy as np

from .paths import LEDGER_DIR


def spec_id(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def _clean(x):
    """JSON has no NaN: write null (and read it back as NaN)."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _revive(summary: dict) -> dict:
    return {k: (float("nan") if v is None else v) for k, v in summary.items()}


class Ledger:
    def __init__(self, directory: str = LEDGER_DIR):
        self.dir = str(directory)
        os.makedirs(os.path.join(self.dir, "vectors"), exist_ok=True)
        self.path = os.path.join(self.dir, "trials.jsonl")
        self.skipped_lines = 0

    def _read(self, f) -> list[dict]:
        out = []
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                warnings.warn(f"ledger: skipping an unreadable line in {self.path}")
                continue
            e["summary"] = _revive(e.get("summary", {}))
            out.append(e)
        return out

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return self._read(f)

    def get(self, tid: str) -> dict | None:
        return next((e for e in self.entries() if e["id"] == tid), None)

    def add(self, spec: dict, summary: dict, per_launch: np.ndarray, selected: np.ndarray, note: str = "", agent: str = "") -> dict:
        """Record a trial once. Re-running the identical spec returns the existing record and does not count again."""
        tid = spec_id(spec)
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            raw = f.read()
            for e in self._read(raw.splitlines()):
                if e["id"] == tid:
                    return e
            vec = os.path.join(self.dir, "vectors", f"{tid}.npz")
            tmp = f"{vec}.{os.getpid()}.tmp.npz"
            np.savez_compressed(tmp, per_launch=per_launch, selected=selected)
            os.replace(tmp, vec)
            entry = {"id": tid, "spec": spec, "summary": summary, "note": note, "agent": agent, "time": time.time()}
            f.seek(0, 2)
            if raw and not raw.endswith("\n"):
                f.write("\n")  # a torn last line must not swallow this entry
            f.write(json.dumps(_clean(entry), allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return entry

    def vectors(self, tid: str) -> tuple[np.ndarray, np.ndarray]:
        z = np.load(os.path.join(self.dir, "vectors", f"{tid}.npz"))
        return z["per_launch"], z["selected"]
