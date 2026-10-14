"""Every trial ever run, appended to a file nobody edits: the count of trials is what makes the final verdict honest."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time

import numpy as np


def spec_id(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


class Ledger:
    def __init__(self, directory: str = "research/ledger"):
        self.dir = directory
        os.makedirs(os.path.join(directory, "vectors"), exist_ok=True)
        self.path = os.path.join(directory, "trials.jsonl")

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def get(self, tid: str) -> dict | None:
        return next((e for e in self.entries() if e["id"] == tid), None)

    def add(self, spec: dict, summary: dict, per_launch: np.ndarray, selected: np.ndarray, note: str = "", agent: str = "") -> dict:
        """Record a trial once. Re-running the identical spec returns the existing record and does not count again."""
        tid = spec_id(spec)
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            for line in f:
                if line.strip() and json.loads(line)["id"] == tid:
                    return json.loads(line)
            entry = {"id": tid, "spec": spec, "summary": summary, "note": note, "agent": agent, "time": time.time()}
            np.savez_compressed(os.path.join(self.dir, "vectors", f"{tid}.npz"), per_launch=per_launch, selected=selected)
            f.seek(0, 2)
            f.write(json.dumps(entry) + "\n")
            f.flush()
            return entry

    def vectors(self, tid: str) -> tuple[np.ndarray, np.ndarray]:
        z = np.load(os.path.join(self.dir, "vectors", f"{tid}.npz"))
        return z["per_launch"], z["selected"]
