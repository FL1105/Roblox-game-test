"""Agents the gym can run: a lazy floor, answers from a file, or any command that reads prompts and writes answers."""

from __future__ import annotations

import json
import shlex
import subprocess


def lazy(prompts: list[dict]) -> dict:
    """The floor: everything is fine, every number is 0. A real agent has to beat this."""
    out: dict = {}
    for p in prompts:
        out[p["id"]] = [] if p["family"] == "audit" else "COMPLY" if p["family"] == "rules" else 0
    return out


def from_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def from_command(cmd: str, prompts: list[dict], timeout: int = 7200) -> dict:
    """Run `cmd` (no shell) with the prompts as JSON on stdin; it must print a JSON object {task id: answer} on stdout."""
    r = subprocess.run(shlex.split(cmd), input=json.dumps(prompts), capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"agent command failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def answers_for(spec: str, prompts: list[dict]) -> dict:
    if spec == "lazy":
        return lazy(prompts)
    if spec.startswith("file:"):
        return from_file(spec[5:])
    if spec.startswith("cmd:"):
        return from_command(spec[4:], prompts)
    raise SystemExit(f"unknown agent {spec!r}: use lazy, file:PATH or cmd:COMMAND")
