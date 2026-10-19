"""python -m gym {prompts,grade,run,report}: generate a round, grade answers, run an agent over rounds, show the ladder."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import agents, ledger
from .tasks import generate


def _print_summary(title: str, s: dict) -> None:
    print(f"\n{title}: {s['overall']}  ->  L{s['level']} {s['name']}")
    print("  rounds: " + ", ".join(f"seed {a}: {b}" for a, b in s["rounds"]))
    for k, v in s["families"].items():
        print(f"  family {k:<8} {v['score']:>5}  (n={v['n']})")
    for k, v in s["skills"].items():
        print(f"  skill  {k:<26} {v['score']:>5}  L{v['level']} {v['name']}  (n={v['n']})")
    if s["used"]:
        print("  skills the bot invoked: " + ", ".join(f"{k} x{v}" for k, v in sorted(s["used"].items(), key=lambda x: -x[1])))
        print(f"  cost: ${s['cost']}   voided answers: {s['voided']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gym")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompts", help="write the prompts for a seed (no answers in them)")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--tier", choices=["base", "hard"], default="base")
    p.add_argument("--out")
    g = sub.add_parser("grade", help="grade an answers file for a seed and record it")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--answers", required=True)
    g.add_argument("--agent", required=True)
    g.add_argument("--split", choices=["dev", "sealed"], default="dev")
    g.add_argument("--tier", choices=["base", "hard"], default="base")
    g.add_argument("--reveal", action="store_true", help="show the expected answer for misses (dev seeds only)")
    g.add_argument("--no-record", action="store_true")
    r = sub.add_parser("run", help="run an agent over consecutive seeds and record each round")
    r.add_argument("--agent", required=True, help="lazy, file:PATH or cmd:COMMAND")
    r.add_argument("--save", help="directory to write each round's answers to (use a path outside the repo)")
    r.add_argument("--name", help="name to record the agent under (default: the --agent value)")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--rounds", type=int, default=3)
    r.add_argument("--split", choices=["dev", "sealed"], default="dev")
    r.add_argument("--tier", choices=["base", "hard"], default="base")
    rp = sub.add_parser("report", help="show the ladder from the ledger")
    rp.add_argument("--agent")
    a = ap.parse_args(argv)

    if a.cmd == "prompts":
        out = json.dumps([t.public() for t in generate(a.seed, tier=a.tier)], indent=1)
        if a.out:
            with open(a.out, "w") as f:
                f.write(out + "\n")
        else:
            print(out)
        return 0
    if a.cmd == "grade":
        if a.reveal and a.split == "sealed":
            raise SystemExit("sealed seeds never reveal answers")
        tasks = generate(a.seed, tier=a.tier)
        with open(a.answers) as f:
            answers = json.load(f)
        rows = ledger.score_round(tasks, answers)
        for t, row in zip(tasks, rows):
            mark = "ok " if row["score"] >= 1 else "part" if row["score"] > 0 else "MISS"
            line = f"{mark} {t.id:<18} {row['score']:.2f}"
            if a.reveal and row["score"] < 1:
                line += f"   expected {t.truth!r}   got {answers.get(t.id)!r}"
            print(line)
        print(f"round score: {ledger.pct(rows):.1f}")
        if not a.no_record:
            ledger.record(a.agent, a.seed, a.split, rows, a.tier)
        return 0
    if a.cmd == "run":
        for i in range(a.rounds):
            seed = a.seed + i
            tasks = generate(seed, tier=a.tier)
            answers = agents.answers_for(a.agent, [t.public() for t in tasks])
            if a.save:
                os.makedirs(a.save, exist_ok=True)
                with open(os.path.join(a.save, f"{a.name or 'agent'}-{a.tier}-{seed}-{a.split}.json"), "w") as f:
                    json.dump(answers, f, indent=1)
            e = ledger.record(a.name or a.agent, seed, a.split, ledger.score_round(tasks, answers), a.tier)
            print(f"seed {seed}: {e['score']}")
        return 0
    es = [e for e in ledger.load() if not a.agent or e["agent"] == a.agent]
    for ag in sorted({e["agent"] for e in es}):
        for tier in ("base", "hard"):
            for split in ("dev", "sealed"):
                sel = [e for e in es if e["agent"] == ag and e.get("tier", "base") == tier and e["split"] == split]
                if sel:
                    _print_summary(f"{ag} [{tier} {split}]", ledger.summarize(sel))
    if not es:
        print("ledger is empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
