"""Records launches AND every trade, with the wallet that made it, from PumpPortal's data stream.

    PUMPPORTAL_API_KEY=... python -m sniper.live --stream --out data/raw_stream.jsonl --minutes 240

PumpPortal only serves the trade feed to a connection that carries an API key funded with at least 0.02 SOL. This program uses the key for READING only:
it sends subscribe and unsubscribe messages and nothing else, and has no code that can send a transaction. The key is read from the environment, is never
written to the file or the log, and is scrubbed from any error text.

What it gives that the price-only recorder (collector.py) cannot: the state of the curve at launch and after every single trade from the first second (no waiting
for a Jupiter route), who traded, which side, how much, and whether the creator sold.

Rows written (one JSON object per line, compatible with the existing file format where they overlap):
  create  {"k":"create","t","mint","creator","symbol","name","dev_tokens","dev_sol","v_sol","v_tokens","mayhem","sig"}
  trade   {"k":"trade","t","mint","trader","side","sol","tokens","v_sol","v_tokens","sig"}
  gap / resume   outages of the feed
"""

from __future__ import annotations

import asyncio
import json
import os
import time

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
OBSERVE_S = 600          # follow each token's trades for this long after launch, then unsubscribe


class MissingKey(RuntimeError):
    pass


def scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


class StreamRecorder:
    def __init__(self, path: str, clock=time.time, observe_s: float = OBSERVE_S):
        self._f = open(path, "a")
        self.clock, self.observe_s = clock, observe_s
        self.active: dict[str, float] = {}      # mint -> launch time (s) while we are following it
        self.stats = {"creates": 0, "trades": 0, "mayhem": 0}

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def write(self, row: dict) -> None:
        self._f.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()

    def on_message(self, msg: dict) -> str | None:
        """Handle one message from the feed. Returns a mint to subscribe to when the message was a new launch."""
        tx, mint = msg.get("txType"), msg.get("mint")
        if not mint or msg.get("pool", "pump") != "pump":
            return None
        try:
            if tx == "create":
                if mint in self.active:
                    return None
                self.stats["creates"] += 1
                mayhem = bool(msg.get("is_mayhem_mode"))
                self.stats["mayhem"] += mayhem
                self.write({"k": "create", "t": self.now_ms(), "mayhem": mayhem, "mint": mint, "creator": msg.get("traderPublicKey"), "symbol": msg.get("symbol") or "",
                            "name": msg.get("name") or "", "dev_tokens": float(msg.get("initialBuy") or 0), "dev_sol": float(msg.get("solAmount") or 0),
                            "v_sol": float(msg["vSolInBondingCurve"]), "v_tokens": float(msg["vTokensInBondingCurve"]), "sig": msg.get("signature")})
                self.active[mint] = self.clock()
                return mint
            if tx in ("buy", "sell") and mint in self.active:
                self.stats["trades"] += 1
                self.write({"k": "trade", "t": self.now_ms(), "mint": mint, "trader": msg.get("traderPublicKey"), "side": tx, "sol": float(msg.get("solAmount") or 0),
                            "tokens": float(msg.get("tokenAmount") or 0), "v_sol": float(msg["vSolInBondingCurve"]), "v_tokens": float(msg["vTokensInBondingCurve"]),
                            "sig": msg.get("signature")})
        except (KeyError, TypeError, ValueError):
            return None  # a malformed message is dropped, never fatal
        return None

    def expired(self) -> list[str]:
        cutoff = self.clock() - self.observe_s
        old = [m for m, t in self.active.items() if t < cutoff]
        for m in old:
            del self.active[m]
        return old

    def summary(self) -> str:
        return f"following {len(self.active)} | {self.stats}"


async def run(out_path: str, minutes: float, api_key: str | None = None, *, connect=None, sleep=asyncio.sleep, clock=time.time) -> None:
    """Record until `minutes` are up. A dropped or refused connection never ends the run: retry with backoff, write gap/resume rows, resubscribe."""
    import websockets
    from websockets.exceptions import WebSocketException

    api_key = api_key or os.environ.get("PUMPPORTAL_API_KEY", "")
    if not api_key:
        raise MissingKey("set PUMPPORTAL_API_KEY (an API key funded with at least 0.02 SOL; the key is only used to read the data stream)")
    connect = connect or (lambda: websockets.connect(f"{PUMPPORTAL_URL}?api-key={api_key}", open_timeout=15))
    rec = StreamRecorder(out_path, clock=clock)
    deadline, last_print, delay, in_gap = clock() + minutes * 60, 0.0, 1.0, False
    refused = 0
    try:
        while clock() < deadline:
            try:
                async with connect() as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    if rec.active:  # after an outage, pick the followed tokens up again
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": list(rec.active)}))
                    delay = 1.0
                    if in_gap:
                        rec.write({"k": "resume", "t": rec.now_ms()})
                        in_gap = False
                    while clock() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 30)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw:
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                msg = None
                            if isinstance(msg, dict):
                                if "message" in msg and "txType" not in msg:
                                    text = scrub(str(msg["message"]), api_key)
                                    if "funded" in text or "API key" in text:
                                        refused += 1
                                        print("feed said:", text[:200], flush=True)
                                        if refused >= 3:
                                            raise MissingKey("PumpPortal refuses the trade feed for this key: " + text[:160])
                                new = rec.on_message(msg)
                                if new:
                                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [new]}))
                        old = rec.expired()
                        if old:
                            await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": old}))
                        if clock() - last_print > 60:
                            last_print = clock()
                            print(time.strftime("%H:%M:%S"), rec.summary(), flush=True)
            except MissingKey:
                raise
            except (OSError, asyncio.TimeoutError, WebSocketException) as e:  # a feed failure is an outage, never the end of a long run
                if not in_gap:
                    rec.write({"k": "gap", "t": rec.now_ms(), "why": scrub(repr(e), api_key)[:120]})
                    in_gap = True
                print("feed dropped:", scrub(repr(e), api_key)[:200], f"(retry in {delay:.0f}s)", flush=True)
                await sleep(delay)
                delay = min(delay * 2, 30)
    finally:
        rec.close()
        print("final:", rec.summary(), flush=True)
