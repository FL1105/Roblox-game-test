"""Event sources for the terminal. Each feed calls `emit(event)` until it ends or `stop` is set."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import replace
from typing import Callable

from ..pump import synth
from ..pump.events import Event, NewToken, Trade

Emit = Callable[[Event], None]


def repeat_lap(events: list[Event], lap: int) -> list[Event]:
    """Lap `lap` of a recording: the same prices, moved later in time, with new mint and wallet names (so the bot sees a fresh-looking market)."""
    span = events[-1].t - events[0].t + 60_000
    shift, tag = (lap - 1) * span, f"~{lap}"
    out: list[Event] = []
    for e in events:
        changes: dict = {"t": e.t + shift, "mint": e.mint + tag}
        if isinstance(e, NewToken):
            changes["creator"] = e.creator + tag
        elif isinstance(e, Trade):
            changes["trader"] = e.trader + tag
        out.append(replace(e, **changes))
    return out


class ReplayFeed:
    """Plays recorded events back. Engine time is event time; `speed` only changes how long we sleep.

    With `loop=True` the recording plays again and again (see `repeat_lap`) instead of ending; `on_lap(n)` is told when a new lap starts."""

    def __init__(self, events: list[Event], speed: float = 1.0, max_gap_s: float = 1.0, sleep: Callable[[float], None] = time.sleep, loop: bool = False):
        self.events = sorted(events, key=lambda e: e.t)
        self.speed, self.max_gap_s, self._sleep, self.loop = speed, max_gap_s, sleep, loop
        self.on_lap: Callable[[int], None] | None = None

    def run(self, emit: Emit, stop: threading.Event) -> None:
        lap = 1
        while True:
            events = self.events if lap == 1 else repeat_lap(self.events, lap)
            prev = None
            for ev in events:
                if stop.is_set():
                    return
                if prev is not None and ev.t > prev:
                    self._sleep(min((ev.t - prev) / 1000 / self.speed, self.max_gap_s))
                prev = ev.t
                emit(ev)
            if not self.loop or not self.events or stop.is_set():
                return
            lap += 1
            if self.on_lap:
                self.on_lap(lap)
            self._sleep(self.max_gap_s)  # the 60 s gap between laps, capped like any other quiet stretch


class DemoFeed:
    """Endless SYNTHETIC market (see pump/synth.py). For trying the terminal, not for learning anything."""

    def __init__(self, seed: int = 7, speed: float = 5.0, batch: int = 25, sleep: Callable[[float], None] = time.sleep, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.seed, self.speed, self.batch, self._sleep, self._now = seed, speed, batch, sleep, now_ms

    def run(self, emit: Emit, stop: threading.Event) -> None:
        start, first_id, n = self._now(), 0, 0
        while not stop.is_set():
            events = synth.generate(self.seed + n, self.batch, start, first_id)
            ReplayFeed(events, self.speed, sleep=self._sleep).run(emit, stop)
            start = max(e.t for e in events) + 1000
            first_id += self.batch
            n += 1


class LiveFeed:
    """Real PumpPortal stream through the recording listener. Needs `websockets` and an RPC URL."""

    def __init__(self, listener):
        self.listener = listener

    def run(self, emit: Emit, stop: threading.Event) -> None:
        from ..listener.live import connect_and_run

        async def main() -> None:
            task = asyncio.create_task(connect_and_run(self.listener))
            while not stop.is_set() and not task.done():
                await asyncio.sleep(0.2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self.listener.drain()

        asyncio.run(main())


class TeeSink:
    """Listener sink that forwards every event to the terminal and, optionally, to a JSONL log."""

    def __init__(self, emit: Emit, jsonl=None):
        self._emit, self._jsonl = emit, jsonl

    def write(self, ev: Event) -> None:
        if self._jsonl is not None:
            self._jsonl.write(ev)
        self._emit(ev)


def start_feed(feed, state, stop: threading.Event) -> threading.Thread:
    if hasattr(feed, "on_lap"):
        feed.on_lap = state.set_lap

    def target() -> None:
        try:
            feed.run(state.emit, stop)
            state.set_feed_status("finished" if not stop.is_set() else "stopped")
        except BaseException as e:  # a dead feed must be visible in the UI, never silent
            state.set_feed_status(f"error: {type(e).__name__}: {e}"[:160])

    t = threading.Thread(target=target, name="feed", daemon=True)
    t.start()
    return t
