"""Records real pump.fun launches and their price paths, using only free public sources.

  PumpPortal  -> a message for every new token (free feed)
  Jupiter     -> real executable quotes, polled per token; three quote sizes pin down the curve's exact reserves

Output is a JSONL log of raw observations (see convert.py for turning it into replayable events). It cannot see
WHO traded (that needs an on-chain feed), only how the curve moved and when, at roughly one-second resolution.
Read-only: no wallet, no key, no orders.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from .infer import FEE, reserves_from_probe
from .jupiter import JupiterQuotes

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
PROBE_SOL = 0.1  # one cheap quote per poll; the three-quote snapshot only runs when this changes
GRADUATED_BELOW_TOKENS = 280_000_000
CHECK_SOL = 3.0       # the second quote size used to verify a token follows the standard curve (a big size is more sensitive)
CHECK_TOLERANCE = 0.005  # relative disagreement between the two sizes' states beyond which a token is treated as off-curve


class Recorder:
    def __init__(self, path: str):
        self._f = open(path, "a")

    def write(self, rec: dict) -> None:
        self._f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class Collector:
    def __init__(self, jup: JupiterQuotes, rec, *, observe_s: float = 600, route_wait_s: float = 30, dead_after_s: float = 75,
                 max_tracked: int = 60, clock=time.time, sleep=asyncio.sleep):
        self.jup, self.rec = jup, rec
        self.observe_s, self.route_wait_s, self.dead_after_s, self.max_tracked = observe_s, route_wait_s, dead_after_s, max_tracked
        self.clock, self.sleep = clock, sleep
        self.tasks: dict[str, asyncio.Task] = {}
        self.stats = {"created": 0, "routable": 0, "never_routable": 0, "ended": 0, "off_curve": 0, "mayhem_skipped": 0, "skipped_capacity": 0}
        self.check_errors: list[float] = []  # second-quote prediction misses, per token

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # ---- intake ----
    def on_create(self, msg: dict) -> None:
        mint = msg.get("mint")
        if msg.get("txType") != "create" or not mint or msg.get("pool", "pump") != "pump" or mint in self.tasks:
            return
        if len(self.tasks) >= self.max_tracked:
            self.stats["skipped_capacity"] += 1
            return
        self.stats["created"] += 1
        mayhem = bool(msg.get("is_mayhem_mode"))
        self.rec.write({"k": "create", "t": self.now_ms(), "mayhem": mayhem, "mint": mint, "creator": msg.get("traderPublicKey"), "symbol": msg.get("symbol") or "",
                        "name": msg.get("name") or "", "dev_tokens": float(msg.get("initialBuy") or 0), "dev_sol": float(msg.get("solAmount") or 0),
                        "v_sol": float(msg["vSolInBondingCurve"]), "v_tokens": float(msg["vTokensInBondingCurve"])})
        if mayhem:  # a different launch variant whose quotes do not follow the standard curve: not priceable
            self.stats["mayhem_skipped"] += 1
            return
        k = float(msg["vSolInBondingCurve"]) * float(msg["vTokensInBondingCurve"])
        task = asyncio.get_running_loop().create_task(self._track(mint, k))
        self.tasks[mint] = task
        task.add_done_callback(lambda _t, m=mint: self.tasks.pop(m, None))

    # ---- per-token tracking ----
    def _end(self, mint: str, reason: str) -> None:
        self.stats["ended"] += 1
        self.rec.write({"k": "end", "t": self.now_ms(), "mint": mint, "reason": reason})

    async def _check_curve(self, mint: str, k: float) -> float | None:
        """Is this token on the standard curve? Quote two sizes back to back, turn each into a state using the launch k,
        and compare. On-curve tokens agree to ~0.1%; off-curve ones disagree by percents. A trade can land between the two
        quotes, so a disagreement is retried (twice) before it counts. Returns the last relative disagreement."""
        diff = None
        for _ in range(3):
            a = await self.jup.buy(mint, PROBE_SOL)
            b = await self.jup.buy(mint, CHECK_SOL)
            if a is None or b is None:
                return None
            s1, s2 = reserves_from_probe(k, PROBE_SOL, a), reserves_from_probe(k, CHECK_SOL, b)
            if s1 is None or s2 is None:
                return None
            diff = s2[0] / s1[0] - 1
            if abs(diff) <= CHECK_TOLERANCE:
                return diff
        return diff

    async def _track(self, mint: str, k: float) -> None:
        """One buy quote per poll gives the exact state, because k = v_sol * v_tokens never changes on a pump.fun
        curve. The first observation is cross-checked with a second quote size."""
        born = self.clock()
        probe = None
        while probe is None:  # wait until Jupiter can route it (2-19 s on live launches)
            if self.clock() - born > self.route_wait_s:
                self.stats["never_routable"] += 1
                return self._end(mint, "never_routable")
            probe = await self.jup.buy(mint, PROBE_SOL)
            if probe is None:
                await self.sleep(0.5)
        self.stats["routable"] += 1

        last_probe, last_change, misses, first = None, self.clock(), 0, True
        while self.clock() - born < self.observe_s:
            now = self.clock()
            probe = probe if probe is not None else await self.jup.buy(mint, PROBE_SOL)
            if probe is None:
                misses += 1
                if misses >= 20:
                    return self._end(mint, "no_route")  # left the curve, or the route disappeared
            else:
                misses = 0
                if probe != last_probe:
                    last_probe, last_change = probe, now
                    state = reserves_from_probe(k, PROBE_SOL, probe)
                    if state is not None:
                        # Record the price the moment we have it; verification and the sell check follow as a separate row,
                        # so a request queue never delays the first observation.
                        self.rec.write({"k": "obs", "t": self.now_ms(), "mint": mint, "probe": probe, "v_sol": state[0], "v_tokens": state[1]})
                        if first:
                            first = False
                            err = await self._check_curve(mint, k)
                            if err is not None:
                                self.check_errors.append(err)
                            if err is not None and abs(err) > CHECK_TOLERANCE:
                                self.stats["off_curve"] += 1
                                return self._end(mint, "off_curve")  # quotes do not follow the standard curve: unpriceable
                            back = await self.jup.sell(mint, probe)  # can this token actually be sold, and at what cost?
                            self.rec.write({"k": "chk", "t": self.now_ms(), "mint": mint, "check_err": err, "rt_in_sol": PROBE_SOL, "rt_back_sol": back})
                        if state[1] <= GRADUATED_BELOW_TOKENS:
                            return self._end(mint, "graduated")
            probe = None
            idle = self.clock() - last_change
            if idle > self.dead_after_s:
                return self._end(mint, "idle")
            await self.sleep(1.0 if idle < 10 else 3.0 if idle < 30 else 8.0)  # quiet tokens get few of the scarce requests
        self._end(mint, "window_over")

    # ---- reporting ----
    def summary(self) -> str:
        ce = f"median |check err| {statistics.median(abs(e) for e in self.check_errors) * 100:.3f}% (n={len(self.check_errors)})" if self.check_errors else "check n/a"
        return f"tracking {len(self.tasks)} | {self.stats} | jupiter {self.jup.stats} @ {self.jup.rps:.1f} rps | {ce}"


async def run(out_path: str, minutes: float, rps: float = 12.0, observe_s: float = 600) -> None:
    import websockets

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(24))
    rec, jup = Recorder(out_path), JupiterQuotes(rps=rps)
    col = Collector(jup, rec, observe_s=observe_s)
    deadline, last_print, delay = time.time() + minutes * 60, 0.0, 1.0
    try:
        while time.time() < deadline:
            try:
                async with websockets.connect(PUMPPORTAL_URL, open_timeout=15) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    delay = 1.0
                    while time.time() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 30)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw:
                            try:
                                col.on_create(json.loads(raw))
                            except (ValueError, KeyError, TypeError):
                                pass
                        if time.time() - last_print > 60:
                            last_print = time.time()
                            print(time.strftime("%H:%M:%S"), col.summary(), flush=True)
            except (OSError, websockets.ConnectionClosed) as e:
                print("feed dropped:", e, f"(retry in {delay:.0f}s)", flush=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
    finally:
        for t in list(col.tasks.values()):
            t.cancel()
        await asyncio.gather(*col.tasks.values(), return_exceptions=True)
        rec.close()
        print("final:", col.summary(), flush=True)
