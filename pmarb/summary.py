"""Summarise a watch log: how often cross-venue gaps appear, how big they are and how long they last."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict


def load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def episodes(rows: list[dict], key: str, positive) -> list[tuple[str, int, float]]:
    """Runs of consecutive polls for one pair where `positive(row)` holds: (label, polls in the run, best value in the run)."""
    times = sorted({r["t"] for r in rows})
    index = {t: i for i, t in enumerate(times)}
    by_label: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in rows:
        by_label[r["label"]][index[r["t"]]] = r
    out = []
    for label, seen in by_label.items():
        run: list[float] = []
        last = None
        for i in sorted(seen):
            r = seen[i]
            if positive(r) and (last is None or i == last + 1 or not run):
                run.append(r[key])
            elif positive(r):
                out.append((label, len(run), max(run)))
                run = [r[key]]
            else:
                if run:
                    out.append((label, len(run), max(run)))
                run = []
            last = i
        if run:
            out.append((label, len(run), max(run)))
    return out


def summarise(rows: list[dict]) -> dict:
    out = {"cycles": len({r["t"] for r in rows}), "observations": len(rows), "by_kind": {}}
    for kind in sorted({r["kind"] for r in rows}):
        sub = [r for r in rows if r["kind"] == kind]
        gross = [r["gross"] for r in sub if r["gross"] is not None]
        maker = [r["maker_edge"] for r in sub if r["maker_edge"] is not None]
        nets = [r for r in sub if r["net"] is not None]
        winners = [r for r in nets if r["net"] > 0]
        eps = episodes(sub, "net", lambda r: r["net"] is not None and r["net"] > 0)
        out["by_kind"][kind] = {
            "pairs": len({r["label"] for r in sub}), "observations": len(sub),
            "gross_positive": sum(g > 0 for g in gross), "gross_max": max(gross, default=None),
            "gross_median": statistics.median(gross) if gross else None,
            "net_positive": len(winners), "net_max": max((r["net"] for r in winners), default=None),
            "net_total_if_each_episode_taken_once": sum(m for _, _, m in eps),
            "episodes": len(eps), "episode_polls": sorted((n for _, n, _ in eps), reverse=True)[:10],
            "maker_ge_1c": sum(m >= 0.01 for m in maker), "maker_ge_2c": sum(m >= 0.02 for m in maker), "maker_n": len(maker),
            "top": sorted(winners, key=lambda r: r["net"], reverse=True)[:5],
        }
    return out


def render(s: dict, interval: float) -> str:
    lines = [f"{s['cycles']} polls of {s['observations']} pair-observations (about every {interval:g}s)", ""]
    notes = {"btc15": "NOT riskless: the venues settle on different price sources and start references, so a gap here is the market pricing "
                      "that difference. The hedge pays $0 when Polymarket resolves Up and Kalshi Down (or the mirror image). 'net' below assumes the two agree.",
             "game": "net assumes both venues settle the game identically (cancellation / postponement handling was not compared)."}
    for kind, d in s["by_kind"].items():
        lines += [f"{kind}: {d['pairs']} pairs, {d['observations']} observations", f"  note: {notes.get(kind, '')}",
                  f"  gross gap > 0 before fees: {d['gross_positive']} observations (best {d['gross_max']:+.3f}, median {d['gross_median']:+.3f})",
                  f"  net > 0 after fees (taker both sides): {d['net_positive']} observations in {d['episodes']} episodes",
                  f"  total profit if every episode were taken once at its best moment: ${d['net_total_if_each_episode_taken_once']:.2f}",
                  f"  longest episodes (polls): {d['episode_polls']}",
                  f"  maker-assisted gap (post on one venue, hedge on the other, no fee on the posted leg) >= 1c in {d['maker_ge_1c']} of {d['maker_n']}, >= 2c in {d['maker_ge_2c']}"]
        for r in d["top"]:
            lines.append(f"    top: {r['label']} K {r['k_bid']}/{r['k_ask']} P {r['p_bid']}/{r['p_ask']} net ${r['net']:.2f} on {r['qty']:g}")
        lines.append("")
    return "\n".join(lines)
