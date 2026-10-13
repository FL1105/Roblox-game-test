"""Live pump.fun recorder. Watches new launches, follows each one's trades for a fixed window,
scans it, and writes everything to a JSONL log the paper trader can replay. It never trades
and never sees a key.

`Listener` talks to any connection with `await send(str)` and `async for raw in conn`, so it is
tested with a fake. The real websocket wiring lives in `connect_and_run`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Callable

from ..pump.events import NewToken, ScanNote, Trade, encode, parse_pumpportal
from ..pump.store import LaunchStore, OutcomeTracker

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"

# scan_fn(launch) -> (verdict, score, summary). Blocking; run in a worker thread.
ScanFn = Callable[[NewToken], "tuple[str, int, str]"]


class JsonlSink:
    def __init__(self, path: str):
        self._f = open(path, "a")

    def write(self, ev) -> None:
        self._f.write(encode(ev) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class Listener:
    def __init__(
        self,
        sink,
        store: LaunchStore,
        scan_fn: ScanFn,
        *,
        observe_s: float = 600,
        max_scans_per_min: int = 30,
        clock: Callable[[], float] = time.time,
    ):
        self.sink, self.store, self.scan_fn = sink, store, scan_fn
        self.observe_ms = int(observe_s * 1000)
        self.max_scans_per_min = max_scans_per_min
        self.clock = clock
        self.trackers: dict[str, OutcomeTracker] = {}
        self.deadlines: deque[tuple[int, str]] = deque()
        self._scan_times: deque[float] = deque()
        self._tasks: set[asyncio.Task] = set()
        self.stats = {"launches": 0, "trades": 0, "scans": 0, "skipped_scans": 0}

    async def on_connect(self, ws) -> None:
        """(Re)subscribe. After a reconnect, mints still being observed are resubscribed too."""
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        if self.trackers:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": list(self.trackers)}))

    async def run(self, ws) -> None:
        await self.on_connect(ws)
        async for raw in ws:
            await self.handle(ws, raw)

    async def handle(self, ws, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        ev = parse_pumpportal(msg, int(self.clock() * 1000))
        if ev is None:
            return
        await self._expire(ws, ev.t)

        if isinstance(ev, NewToken):
            if ev.mint in self.trackers:
                return
            self.stats["launches"] += 1
            self.sink.write(ev)
            self.store.add_launch(ev)
            self.trackers[ev.mint] = OutcomeTracker(ev)
            self.deadlines.append((ev.t + self.observe_ms, ev.mint))
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [ev.mint]}))
            task = asyncio.create_task(self._scan(ev))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif isinstance(ev, Trade):
            tracker = self.trackers.get(ev.mint)
            if tracker is None:
                return
            self.stats["trades"] += 1
            self.sink.write(ev)
            tracker.on_trade(ev)

    async def drain(self) -> None:
        """Wait for in-flight scans (used on shutdown and in tests)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _expire(self, ws, now_ms: int) -> None:
        done = []
        while self.deadlines and self.deadlines[0][0] <= now_ms:
            _, mint = self.deadlines.popleft()
            tracker = self.trackers.pop(mint, None)
            if tracker:
                self.store.add_outcome(mint, tracker.finalize())
                done.append(mint)
        if done:
            await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": done}))

    async def _scan(self, launch: NewToken) -> None:
        now = self.clock()
        while self._scan_times and now - self._scan_times[0] > 60:
            self._scan_times.popleft()
        if len(self._scan_times) >= self.max_scans_per_min:
            self.stats["skipped_scans"] += 1
            self.sink.write(ScanNote(int(now * 1000), launch.mint, "SKIPPED", 0, "scan rate limit"))
            return
        self._scan_times.append(now)
        try:
            verdict, score, summary = await asyncio.get_running_loop().run_in_executor(None, self.scan_fn, launch)
        except Exception as e:  # a failed scan must never kill the recorder; it is logged as ERROR
            verdict, score, summary = "ERROR", 0, f"{type(e).__name__}: {e}"[:200]
        self.stats["scans"] += 1
        self.sink.write(ScanNote(int(self.clock() * 1000), launch.mint, verdict, score, summary))


async def connect_and_run(listener: Listener, url: str = PUMPPORTAL_URL) -> None:
    """Run forever, reconnecting with capped exponential backoff."""
    try:
        import websockets
    except ImportError as e:
        raise SystemExit("The live listener needs the 'websockets' package: pip install websockets") from e

    delay = 1.0
    while True:
        try:
            async with websockets.connect(url) as ws:
                delay = 1.0
                await listener.run(ws)
        except (OSError, websockets.ConnectionClosed) as e:
            print(f"connection lost ({e}); retrying in {delay:.0f}s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)
