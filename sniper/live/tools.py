"""Small CLI: python -m sniper.live.tools events RAW... --out events.jsonl   (raw recordings -> replayable events)"""

import argparse

from ..pump.events import write_jsonl
from .convert import read_raw, to_events


def main() -> int:
    p = argparse.ArgumentParser(prog="sniper.live.tools")
    p.add_argument("cmd", choices=["events"])
    p.add_argument("raw", nargs="+")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    rows = [r for path in a.raw for r in read_raw(path)]
    events = to_events(rows)
    write_jsonl(a.out, events)
    print(f"{len(events)} events from {len(rows)} raw rows -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
