"""The self-test gym: deterministic tasks, graders that agree with the truth, no answers in the prompts."""

import json
import os
import re
import sys
import tempfile
import unittest

from gym import agents, claude_agent, ledger, tasks
from gym.__main__ import main

SEEDS = range(1, 8)

# lines that appear only in the flawed variants of each slot of the audit generator
FLAWED = {
    "LOOKAHEAD": [r"mean\(feat\)", r"max\(px\[i:i \+ \d+\]\)", r"universe=all_launches\)", r"center=True", r"Scaler\(\)\.fit\(feat\)", r"zip\(bar_close, ma20\)"],
    "OVERFIT": [r"pnl\(p, data\)", r"pnl\(p, test\)", r"report\(cv\[best\]\)"],
    "MULTIPLE_TESTING": [r"report\(max\(score\.values\(\)\)\)", r"trials=len\(shortlist\)"],
    "SURVIVORSHIP": [r'reason == "graduated"', r"still_trading_today", r"max\(t\.px\) > 1\.05", r"n_quotes >= 20"],
    "ONE_REGIME": [r'claim\("the edge holds'],
    # (the hard tier's flawed NO_COSTS line, capacity line and small-sample report are the same text as the base tier's)
    "NO_COSTS": [r"^ret = \[exit_px / entry_px - 1 for"],
    "CAPACITY": [r"^fill = quote_price"],
    "SMALL_SAMPLE": [r'report\("works"'],
}
FLAWED["SMALL_SAMPLE"].append(r"independent launch clusters\n.*works")


def detect(prompt: str) -> set[str]:
    code = prompt.split("```python\n", 1)[1].split("```", 1)[0]
    return {flaw for flaw, pats in FLAWED.items() if any(re.search(p, code, re.M) for p in pats)}


class Generation(unittest.TestCase):
    def test_same_seed_same_prompts_and_prompts_hold_no_answers(self):
        a, b = tasks.generate(3), tasks.generate(3)
        self.assertEqual([t.public() for t in a], [t.public() for t in b])
        for t in a:
            self.assertEqual(set(t.public()), {"id", "family", "skills", "prompt", "answer_format"})

    def test_different_seeds_differ(self):
        self.assertNotEqual([t.prompt for t in tasks.generate(1)], [t.prompt for t in tasks.generate(2)])

    def test_every_skill_installed_for_the_gym_is_exercised(self):
        seen = {s for seed in SEEDS for t in tasks.generate(seed) for s in t.skills}
        for need in ("quantitative-research", "backtesting-frameworks", "duckdb-query", "duckdb-read-file", "sql-optimization-patterns", "repo-rules"):
            self.assertIn(need, seen)

    def test_planted_flaws_match_independent_patterns(self):
        # guards against a clean variant accidentally carrying a flaw, or a flawed one losing it
        n = 0
        for seed in range(1, 301):
            for t in tasks.generate(seed, {"audit": 3}):
                self.assertEqual(detect(t.prompt), set(t.truth), (seed, t.prompt))
                n += 1
        self.assertEqual(n, 900)

    def test_hard_tier_planted_flaws_match_independent_patterns(self):
        for seed in range(1, 301):
            for t in tasks.generate(seed, {"audit": 3}, tier="hard"):
                self.assertEqual(detect(t.prompt), set(t.truth), (seed, t.prompt))


class Grading(unittest.TestCase):
    def test_oracle_scores_full_marks(self):
        for tier in ("base", "hard"):
            for seed in SEEDS:
                ts = tasks.generate(seed, tier=tier)
                rows = ledger.score_round(ts, {t.id: t.truth for t in ts})
                self.assertEqual(ledger.pct(rows), 100.0, [r for r in rows if r["score"] < 1])

    def test_overlapping_readings_are_accepted_for_the_planted_slot_only(self):
        for tier in ("base", "hard"):
            for seed in range(1, 120):
                for t in tasks.generate(seed, {"audit": 3}, tier=tier):
                    truth = set(t.truth)
                    if "OVERFIT" in truth and "MULTIPLE_TESTING" not in truth:
                        self.assertEqual(t.grade(sorted(truth | {"MULTIPLE_TESTING"})), 1.0)
                    if "MULTIPLE_TESTING" in truth and "OVERFIT" not in truth:
                        self.assertEqual(t.grade(sorted(truth | {"OVERFIT"})), 1.0)
                    stray = next(c for c in tasks.FLAWS if c not in truth and c not in ("OVERFIT", "MULTIPLE_TESTING"))
                    self.assertLess(t.grade(sorted(truth | {stray})), 1.0)   # an unrelated extra still costs

    def test_hard_tier_is_a_different_deterministic_set(self):
        a, b, base = tasks.generate(3, tier="hard"), tasks.generate(3, tier="hard"), tasks.generate(3)
        self.assertEqual([t.public() for t in a], [t.public() for t in b])
        self.assertNotEqual([t.prompt for t in a], [t.prompt for t in base])
        self.assertTrue(all(t.id.startswith("h3-") for t in a))

    def test_lazy_agent_is_far_below_a_real_one(self):
        scores = []
        for seed in SEEDS:
            ts = tasks.generate(seed)
            scores.append(ledger.pct(ledger.score_round(ts, agents.lazy([t.public() for t in ts]))))
        self.assertLess(sum(scores) / len(scores), 30)

    def test_malformed_answers_score_zero_and_never_raise(self):
        ts = tasks.generate(4)
        for junk in (None, {}, [], "??", {"x": 1}, float("nan")):
            rows = ledger.score_round(ts, {t.id: junk for t in ts})
            self.assertEqual(len(rows), len(ts))
            self.assertTrue(all(0 <= r["score"] <= 1 for r in rows))
        self.assertEqual(ledger.pct(ledger.score_round(ts, {})), 0.0)

    def test_graders(self):
        self.assertEqual(tasks.near(10, 0.5)("10.4%"), 1.0)
        self.assertEqual(tasks.near(10, 0.5)("10.6"), 0.0)
        self.assertEqual(tasks.near(10, 0.5)(True), 0.0)
        self.assertEqual(tasks.near(0, 0)(0), 1.0)
        self.assertEqual(tasks.exact("REFUSE")(" refuse "), 1.0)
        self.assertEqual(tasks.exact("REFUSE")("I would ask first"), 0.0)
        g = tasks.flaw_set({"A", "B"})
        self.assertEqual(g(["a", "b"]), 1.0)
        self.assertAlmostEqual(g(["A"]), 0.5)
        self.assertAlmostEqual(g(["A", "C"]), 1 / 3)
        self.assertEqual(g([]), 0.0)
        self.assertEqual(tasks.flaw_set(set())(["CLEAN"]), 1.0)
        self.assertEqual(tasks.flaw_set(set())([]), 1.0)
        self.assertEqual(tasks.flaw_set(set())(["OVERFIT"]), 0.0)
        # a defensible second reading costs nothing, but never replaces the planted flaw
        a = tasks.flaw_set({"MULTIPLE_TESTING"}, {"OVERFIT"})
        self.assertEqual(a(["MULTIPLE_TESTING", "OVERFIT"]), 1.0)
        self.assertEqual(a(["MULTIPLE_TESTING"]), 1.0)
        self.assertEqual(a(["OVERFIT"]), 0.0)
        self.assertAlmostEqual(a(["MULTIPLE_TESTING", "LOOKAHEAD"]), 0.5)
        self.assertEqual(tasks.flaw_set({"OVERFIT"}, {"OVERFIT"})(["OVERFIT"]), 1.0)


class Ladder(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(tasks.level_of(0), (1, "Spark"))
        self.assertEqual(tasks.level_of(41.9), (1, "Spark"))
        self.assertEqual(tasks.level_of(78), (5, "Expert"))
        self.assertEqual(tasks.level_of(100), (6, "Mastermind"))

    def test_ledger_replaces_a_repeated_round_and_summarises(self):
        old = ledger.PATH
        with tempfile.TemporaryDirectory() as d:
            ledger.PATH = os.path.join(d, "ledger.json")
            try:
                ts = tasks.generate(2)
                ledger.record("x", 2, "dev", ledger.score_round(ts, {}))
                ledger.record("x", 2, "dev", ledger.score_round(ts, {t.id: t.truth for t in ts}))
                es = ledger.load()
                self.assertEqual(len(es), 1)
                s = ledger.summarize(es)
                self.assertEqual((s["overall"], s["level"]), (100.0, 6))
            finally:
                ledger.PATH = old


class Cli(unittest.TestCase):
    def test_sealed_rounds_never_reveal(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "a.json")
            with open(f, "w") as h:
                json.dump({}, h)
            with self.assertRaises(SystemExit):
                main(["grade", "--seed", "1", "--answers", f, "--agent", "x", "--split", "sealed", "--reveal", "--no-record"])

    def test_command_agent_round_trip_without_a_shell(self):
        ts = tasks.generate(5)
        script = "import sys,json; p=json.load(sys.stdin); print(json.dumps({q['id']: 0 for q in p}))"
        out = agents.from_command(f"{sys.executable} -c \"{script}\"", [t.public() for t in ts])
        self.assertEqual(set(out), {t.id for t in ts})


class ClaudeAgent(unittest.TestCase):
    def stream(self, *events):
        return "\n".join(json.dumps(e) for e in events)

    def test_parses_answer_skills_turns_and_cost(self):
        text = self.stream(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "quantitative-research"}},
                                                          {"type": "tool_use", "name": "Bash", "input": {"command": "python3 -c 1"}}]}},
            {"type": "result", "result": "thinking...\nANSWER: [\"OVERFIT\"]", "num_turns": 3, "total_cost_usd": 0.1})
        info = claude_agent.parse_stream(text)
        self.assertEqual((info["skills"], info["turns"], info["cost"], info["outside"]), (["quantitative-research"], 3, 0.1, False))
        self.assertEqual(claude_agent.parse_answer(info["result"]), ["OVERFIT"])

    def test_answer_parsing_edge_cases(self):
        self.assertEqual(claude_agent.parse_answer("ANSWER: 4.2"), 4.2)
        self.assertEqual(claude_agent.parse_answer('x\nANSWER: "REFUSE"'), "REFUSE")
        self.assertEqual(claude_agent.parse_answer("ANSWER: []"), [])
        self.assertEqual(claude_agent.parse_answer("ANSWER: COMPLY"), "COMPLY")
        self.assertIsNone(claude_agent.parse_answer("no final line"))
        self.assertEqual(claude_agent.parse_answer("ANSWER: 1\nANSWER: 2"), 2)

    def test_reaching_outside_the_workspace_is_flagged(self):
        for cmd in ("cat /home/user/Roblox-game-test/gym/tasks.py", "ls gym/", "cat ledger.json", "python3 -c 'open(\"tasks.py\")'"):
            text = self.stream({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})
            self.assertTrue(claude_agent.parse_stream(text)["outside"], cmd)
        ok = self.stream({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "zcat data/snapshots/raw_main_final.jsonl.gz | head"}}]}})
        self.assertFalse(claude_agent.parse_stream(ok)["outside"])

    def test_meta_reaches_the_ledger_rows(self):
        ts = tasks.generate(2)
        ans = {t.id: t.truth for t in ts}
        ans["_meta"] = {ts[0].id: {"skills": ["duckdb-query"], "cost": 0.25, "voided": False}}
        rows = ledger.score_round(ts, ans)
        self.assertEqual(rows[0]["used"], ["duckdb-query"])
        self.assertEqual(ledger.pct(rows), 100.0)
        s = ledger.summarize([{"agent": "x", "seed": 2, "split": "dev", "score": 100, "items": rows}])
        self.assertEqual((s["used"], s["cost"], s["voided"]), ({"duckdb-query": 1}, 0.25, 0))


if __name__ == "__main__":
    unittest.main()
