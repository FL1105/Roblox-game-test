"""One command, one fixed method: from raw recordings to an honest written evaluation.

    python -m sniper.report data/raw_main.jsonl [more raw files] --out data/report.md

Launches are ordered by time. The earlier 70% train the learned model; everything under "unseen launches" uses ONLY
the later 30%, which no model or setting has ever been fitted to. The smart-exit rules use their stock parameters,
chosen before looking at any results, so they have no advantage there either.
"""

from __future__ import annotations

import argparse
import sys
import time

from .live.analyze import report as describe
from .live.convert import read_raw, to_events, token_paths
from .paper.compare import ablation_table, compare
from .policy.model import HoldModel
from .policy.train import split_tokens, train
from .pump.events import Event

COSTS = (0.0, 0.001, 0.003)  # SOL per transaction side: free, cheap, realistic for a contested launch


def events_for(events: list[Event], paths: list[dict]) -> list[Event]:
    mints = {p["create"]["mint"] for p in paths}
    return [e for e in events if e.mint in mints]


def build(raw_paths: list[str], test_frac: float = 0.3, model_out: str | None = None) -> str:
    rows = [r for path in raw_paths for r in read_raw(path)]
    paths = token_paths(rows)
    events = to_events(rows)
    train_p, test_p = split_tokens(paths, test_frac)
    ev_train, ev_test = events_for(events, train_p), events_for(events, test_p)
    span_min = (max(p["create"]["t"] for p in paths.values()) - min(p["create"]["t"] for p in paths.values())) / 60000 if paths else 0
    out = [f"# Evaluation on real recorded launches", f"generated {time.strftime('%Y-%m-%d %H:%M')} from {len(raw_paths)} file(s); {len(paths)} priceable launches over {span_min:.0f} minutes; "
           f"{len(train_p)} earlier launches for fitting, {len(test_p)} later ones held out", ""]
    out += ["## What happens to a blindly sniped launch", "```", describe(paths), "```", ""]

    model = None
    out += ["## Learned hold/sell model", "```"]
    try:
        model, text = train(paths, test_frac)
        out.append(text)
        if model_out:
            model.save(model_out)
            out.append(f"saved {model_out}")
    except SystemExit as e:
        out.append(str(e))
    out += ["```", ""]

    out += ["## Strategies on UNSEEN launches (the later part only)", ""]
    for cost in COSTS:
        out += [f"### transaction cost {cost} SOL per side", "```", compare(ev_test, model, tx_cost_sol=cost), "```", ""]
    out += ["## Which smart-exit rules matter? (unseen launches, no transaction cost)", "```", ablation_table(ev_test, 0.0), "```", ""]
    out += ["## For reference: the same strategies on the EARLIER launches (the model was fitted here, so this flatters it)", "```", compare(ev_train, model, tx_cost_sol=0.0), "```", ""]
    out += ["## How to read this", "- `mean ret` is the average net return per trade after fees and costs; the 95% interval is a bootstrap and needs 20+ trades to appear.",
            "- A strategy only counts if its interval on UNSEEN launches sits above zero, at a cost level you can actually achieve.",
            "- The recorder cannot see who traded, so creator-sell exits cannot fire, and entries come several seconds after launch.",
            "- A few hundred launches is a small sample: treat anything short of a clear, repeated win as 'no edge shown'."]
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sniper.report")
    p.add_argument("raw", nargs="+")
    p.add_argument("--out", default="report.md")
    p.add_argument("--test-frac", type=float, default=0.3)
    p.add_argument("--model-out", help="also save the trained model here")
    a = p.parse_args(argv)
    text = build(a.raw, a.test_frac, a.model_out)
    with open(a.out, "w") as f:
        f.write(text + "\n")
    print(text)
    print(f"\nwritten to {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
