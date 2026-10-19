"""Scores per round, per family and per skill, kept in gym/ledger.json, with the ladder level each skill has reached."""

from __future__ import annotations

import fcntl
import json
import os
import statistics
from collections import defaultdict

from .tasks import Task, level_of

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.json")


def score_round(tasks: list[Task], answers: dict) -> list[dict]:
    """One row per task. A missing or malformed answer scores 0; a grader that raises scores 0."""
    rows = []
    meta = answers.get("_meta", {}) if isinstance(answers.get("_meta"), dict) else {}
    for t in tasks:
        try:
            s = float(t.grade(answers.get(t.id)))
        except Exception:
            s = 0.0
        row = {"id": t.id, "family": t.family, "skills": t.skills, "score": s}
        m = meta.get(t.id)
        if isinstance(m, dict):
            row.update({"used": m.get("skills", []), "cost": m.get("cost", 0), "voided": bool(m.get("voided"))})
        rows.append(row)
    return rows


def pct(rows: list[dict]) -> float:
    return 100 * statistics.mean(r["score"] for r in rows) if rows else 0.0


def load() -> list[dict]:
    if not os.path.exists(PATH):
        return []
    with open(PATH) as f:
        return json.load(f)


def record(agent: str, seed: int, split: str, rows: list[dict], tier: str = "base") -> dict:
    """Append a round; a second record of the same (agent, tier, seed, split) replaces the first."""
    entry = {"agent": agent, "tier": tier, "seed": seed, "split": split, "score": round(pct(rows), 1), "items": rows}
    with open(PATH + ".lock", "w") as lock:          # several runs may record at once
        fcntl.flock(lock, fcntl.LOCK_EX)
        kept = [e for e in load() if (e["agent"], e.get("tier", "base"), e["seed"], e["split"]) != (agent, tier, seed, split)]
        kept.append(entry)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(kept, f, indent=1)
            f.write("\n")
        os.replace(tmp, PATH)
    return entry


def summarize(entries: list[dict]) -> dict:
    fam: dict[str, list[float]] = defaultdict(list)
    skl: dict[str, list[float]] = defaultdict(list)
    for e in entries:
        for r in e["items"]:
            fam[r["family"]].append(r["score"])
            for s in r["skills"]:
                skl[s].append(r["score"])
    def pack(d: dict) -> dict:
        out = {}
        for k, v in sorted(d.items()):
            sc = 100 * statistics.mean(v)
            lv, name = level_of(sc)
            out[k] = {"score": round(sc, 1), "n": len(v), "level": lv, "name": name}
        return out
    used: dict[str, int] = defaultdict(int)
    for e in entries:
        for r in e["items"]:
            for u in r.get("used", []):
                used[u] += 1
    allscores = [r["score"] for e in entries for r in e["items"]]
    overall = 100 * statistics.mean(allscores) if allscores else 0.0
    lv, name = level_of(overall)
    return {"overall": round(overall, 1), "level": lv, "name": name, "families": pack(fam), "skills": pack(skl),
            "rounds": [(e["seed"], e["score"]) for e in entries], "used": dict(used),
            "cost": round(sum(r.get("cost", 0) for e in entries for r in e["items"]), 2),
            "voided": sum(1 for e in entries for r in e["items"] if r.get("voided"))}
