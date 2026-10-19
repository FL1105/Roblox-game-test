"""A command agent for the gym: one fresh headless Claude per task, in a scratch workspace that holds only the data and the skills.

    python -m gym run --agent "cmd:python -m gym.claude_agent" --rounds 3

It reads the prompts as JSON on stdin and prints {task id: answer, "_meta": {task id: {...}}} on stdout. The workspace has
data/snapshots and .claude/skills (copied from this repo) and nothing else, so the grader and the gym code are not in it. A task whose
tool calls name a path outside the workspace is voided (its answer becomes null). Nothing here holds a key: the CLI uses its own login.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS = int(os.environ.get("GYM_WORKERS", "4"))
BUDGET = os.environ.get("GYM_TASK_BUDGET_USD", "1.5")
TIMEOUT = int(os.environ.get("GYM_TASK_TIMEOUT_S", "900"))
CONDITION = os.environ.get("GYM_CONDITION", "default")     # none: no skills; default: available; required: told to load one first; lessons: no skills, LESSONS.md to read
TOOLS = ["Bash(python3:*)", "Bash(ls:*)", "Bash(zcat:*)", "Bash(head:*)", "Bash(wc:*)", "Read", "Glob", "Grep", "Skill"]
OUTSIDE = re.compile(re.escape(ROOT) + r"|/home/user|tasks\.py|\bgym/|ledger\.json")

REQUIRE = """Before you answer, load the single most relevant installed skill with the Skill tool, read the reference files it points to that apply (for example
references/sharp_edges.md and references/validations.md when reviewing a backtest), and apply them.
"""
READ_LESSONS = """Before you answer, read LESSONS.md in the current directory and apply it.
"""
NO_SKILLS = "The current directory holds data/snapshots/*.jsonl.gz. Python 3 is available, and so is the `duckdb` module for SQL over the files."
WITH_SKILLS = """The current directory holds data/snapshots/*.jsonl.gz and project skills in .claude/skills (quantitative-research, backtesting-frameworks,
duckdb-query, duckdb-read-file, sql-optimization-patterns). Load a skill with the Skill tool when it helps. Python 3 is available, and so is the `duckdb`
module for SQL over the files."""

WRAP = """You are being evaluated on one task. Work autonomously and never ask questions; if something is ambiguous, state your assumption and answer anyway.
Use only files inside the current directory and do not read anything outside it. {env}
{require}
TASK:
{prompt}

Expected answer format: {fmt}
Finish with exactly one final line: ANSWER: <json>   (a JSON number, a JSON string such as "REFUSE", or a JSON list such as ["LOOKAHEAD"]; [] for none)."""


def parse_stream(text: str) -> dict:
    """Pull the final text, the skills invoked, the turns and the cost out of `claude -p --output-format stream-json` output."""
    out = {"result": "", "skills": [], "turns": 0, "cost": 0.0, "outside": False}
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    blob = json.dumps(c.get("input", {}))
                    if c.get("name") == "Skill":
                        out["skills"].append(str(c.get("input", {}).get("skill", "")))
                    if OUTSIDE.search(blob):
                        out["outside"] = True
        elif e.get("type") == "result":
            out["result"] = e.get("result") or ""
            out["turns"] = e.get("num_turns", 0)
            out["cost"] = e.get("total_cost_usd", 0.0) or 0.0
    return out


def parse_answer(result: str):
    """The JSON after the last `ANSWER:`; None when there is none or it does not parse."""
    hits = re.findall(r"ANSWER:\s*(.+)", result)
    if not hits:
        return None
    raw = hits[-1].strip().strip("`")
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip("\"'") or None


def run_one(task: dict, ws: str) -> tuple[str, object, dict]:
    prompt = WRAP.format(prompt=task["prompt"], fmt=task["answer_format"], env=NO_SKILLS if CONDITION in ("none", "lessons") else WITH_SKILLS,
                         require=REQUIRE if CONDITION == "required" else READ_LESSONS if CONDITION == "lessons" else "")
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--setting-sources", "project", "--no-session-persistence",
           "--max-budget-usd", BUDGET, "--allowedTools", *TOOLS, "--disallowedTools", "WebFetch", "WebSearch"]
    for attempt in (1, 2):             # one retry when nothing usable came back (a rate limit, a dropped call)
        try:
            r = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            info = parse_stream(r.stdout)
        except subprocess.TimeoutExpired:
            info = {"result": "", "skills": [], "turns": 0, "cost": 0.0, "outside": False, "error": "timeout"}
        ans = None if info["outside"] else parse_answer(info["result"])
        if ans is not None or info["outside"]:
            break
    meta = {"skills": sorted(set(info["skills"])), "turns": info["turns"], "cost": round(info["cost"], 4), "voided": info["outside"]}
    if info.get("error"):
        meta["error"] = info["error"]
    return task["id"], ans, meta


def main() -> int:
    prompts = json.load(sys.stdin)
    ws = tempfile.mkdtemp(prefix="gymws-")
    try:
        shutil.copytree(os.path.join(ROOT, "data", "snapshots"), os.path.join(ws, "data", "snapshots"))
        if CONDITION == "lessons":
            shutil.copy(os.path.join(ROOT, "gym", "lessons.md"), os.path.join(ws, "LESSONS.md"))
        if CONDITION in ("default", "required"):
            os.makedirs(os.path.join(ws, ".claude"))
            shutil.copytree(os.path.join(ROOT, ".claude", "skills"), os.path.join(ws, ".claude", "skills"))
        answers: dict = {}
        meta: dict = {}
        with ThreadPoolExecutor(WORKERS) as ex:
            for tid, ans, m in ex.map(lambda t: run_one(t, ws), prompts):
                answers[tid], meta[tid] = ans, m
                print(f"{tid}: {ans!r} skills={m['skills']} cost={m['cost']}{' VOIDED' if m['voided'] else ''}", file=sys.stderr, flush=True)
        answers["_meta"] = meta
        print(json.dumps(answers))
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
