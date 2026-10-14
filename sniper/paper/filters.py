"""Filter lab: which entry parameters actually help on real launches?

Every candidate is the same base strategy (snipe, smart exit, 0.1 SOL) with ONE entry filter changed. Each is scored on
the earlier launches (where you may pick) and, side by side, on the later unseen launches (where you must confirm).
Trying many settings guarantees that a few look good by luck, so the table says how many were tried and the selected
setting is judged only by its unseen result.
"""

from __future__ import annotations

from dataclasses import replace

from ..pump.events import Event
from .compare import Row, base_strategy, fmt_row, run
from .engine import Strategy

MIN_TRADES_TO_SELECT = 30


def candidates(base: Strategy) -> dict[str, Strategy]:
    c: dict[str, Strategy] = {"baseline (no entry filter)": base}
    for v in (0.5, 1, 2, 5):
        c[f"min liquidity {v} SOL raised"] = replace(base, min_liquidity_sol=v)
    for v in (0, 3, 7):
        c[f"max dev holding {v}%"] = replace(base, max_dev_buy_pct=float(v))
    for v in (1, 2, 4):
        c[f"at least {v} price moves seen"] = replace(base, min_trades=v)
    for v in (15, 30):
        c[f"token younger than {v}s when bought"] = replace(base, max_token_age_s=float(v))
    for v in (5, 20, 60):
        c[f"not already up more than {v}%"] = replace(base, max_entry_gain_pct=float(v))
    for v in (0, 2):
        c[f"creator has at most {v} earlier launches (24h)"] = replace(base, max_creator_launches=v)
    c["skip creators with a collapsed token"] = replace(base, block_collapsed_creators=True)
    c["scanner PASS only"] = replace(base, allowed_verdicts=frozenset({"PASS"}))
    for v in (5, 15):
        c[f"confirm entry: up {v}% with buying"] = replace(base, entry_mode="confirm", confirm_gain_pct=float(v))
    c["combo: 2+ moves, not up >60%, PASS only"] = replace(base, min_trades=2, max_entry_gain_pct=60.0, allowed_verdicts=frozenset({"PASS"}))
    c["combo: creator filters + dev <=3% + PASS"] = replace(base, max_creator_launches=2, block_collapsed_creators=True, max_dev_buy_pct=3.0, allowed_verdicts=frozenset({"PASS"}))
    c["combo: confirm 5% + not up >60% + 1+ move"] = replace(base, entry_mode="confirm", confirm_gain_pct=5.0, max_entry_gain_pct=60.0, min_trades=1)
    return c


def lab(ev_train: list[Event], ev_test: list[Event], tx_cost_sol: float = 0.0, latency_ms: int = 800) -> str:
    base = replace(base_strategy(tx_cost_sol, latency_ms), exit_mode="smart")
    cands = candidates(base)
    results: dict[str, tuple[Row, Row]] = {n: (run(ev_train, s, None, n), run(ev_test, s, None, n)) for n, s in cands.items()}

    def cell(r: Row) -> str:
        ci = f"[{r.ci[0]:+5.1f},{r.ci[1]:+5.1f}]" if r.ci else "     n<20    "
        return f"{r.trades:4} {r.mean_ret:+6.1f}% {ci}"

    lines = [f"{'entry filter (snipe + smart exit)':46} | {'EARLIER launches: n  mean  95% CI':40} | UNSEEN launches: n  mean  95% CI"]
    for name, (tr, te) in results.items():
        lines.append(f"{name:46} | {cell(tr):40} | {cell(te)}")
    lines.append("")
    eligible = [(n, tr, te) for n, (tr, te) in results.items() if tr.trades >= MIN_TRADES_TO_SELECT and n != "baseline (no entry filter)"]
    base_tr, base_te = results["baseline (no entry filter)"]
    if eligible:
        n, tr, te = max(eligible, key=lambda x: x[1].mean_ret)
        lines.append(f"Selected on the earlier launches (best mean with >= {MIN_TRADES_TO_SELECT} trades): {n}")
        lines.append(f"  earlier: {tr.mean_ret:+.2f}% over {tr.trades} trades     unseen: {te.mean_ret:+.2f}% over {te.trades} trades   (baseline unseen: {base_te.mean_ret:+.2f}% over {base_te.trades})")
        verdict = "HOLDS UP" if te.trades >= 20 and te.mean_ret > base_te.mean_ret and te.ci and te.ci[0] > base_te.mean_ret else "does NOT clearly hold up"
        lines.append(f"  -> on unseen launches the selected filter {verdict} (needs 20+ trades and a confidence interval entirely above the baseline's mean)")
    else:
        lines.append(f"No filter kept {MIN_TRADES_TO_SELECT}+ trades on the earlier launches, so nothing can be selected yet; record more data.")
    lines.append(f"{len(results) - 1} settings were tried. A few will look good by chance alone; only an improvement that appears on BOTH sides, with a clear margin, means anything.")
    return "\n".join(lines)
