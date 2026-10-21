"""Live execution: the shared plan (plan.py) carried out with real transactions. Imported only for --trade-live.

Each leg is a Payment from the wallet to itself that converts one asset into the next (partial payment, with `DeliverMin` as the price
guard). Every leg is first run through the server's `simulate`, and is sent only if the simulation succeeds and delivers at least the
guard; then it is signed locally, submitted, and followed until validated. The ledger has no way to make a multi-leg cycle atomic, so
between legs the wallet holds the intermediate asset; the plan bounds what that can cost, and halts the bot if it cannot get out.

The seed is read once from the environment, held only inside the xrpl-py Wallet, and is never logged or put in the dashboard state."""

from __future__ import annotations

import threading
import time

from .assets import Asset, amount_json, amount_value
from .executor import Executor
from .feed import Snapshot
from .paper import fee_in_base
from .plan import Attempt, Outcome, Result, run_plan
from .routes import Evaluation, Route
from .rpc import Rpc, RpcError

TF_PARTIAL_PAYMENT = 0x00020000
CEILING = 10.0  # `Amount` is only a ceiling on what a partial payment may deliver; SendMax is what bounds the spend
LEDGERS_TO_LIVE = 4  # LastLedgerSequence = current + this
MAX_FEE_DROPS = 1000  # refuse to send when the network asks more than this per transaction (about 0.001 XRP)
XRP_BUFFER = 2.0  # XRP always left untouched on top of the reserve, so fees can never be the thing that fails
STATE_REFRESH_S = 60.0


class LiveSetupError(Exception):
    pass


class LiveExecutor(Executor):
    def __init__(self, cfg, base: Asset, state, rpc: Rpc, seed: str, max_session_loss: float | None = None):
        super().__init__(base, state)
        try:
            from xrpl.core.binarycodec import encode  # noqa: F401
            from xrpl.models.transactions import Payment  # noqa: F401
            from xrpl.transaction import sign  # noqa: F401
            from xrpl.wallet import Wallet
        except ImportError:
            raise LiveSetupError("live trading needs the xrpl-py package: pip install xrpl-py")
        try:
            self._wallet = Wallet.from_seed(seed)
        except Exception:  # never echo the seed or the library's message about it
            raise LiveSetupError("XRPL_SEED is not a valid family seed")
        self.address: str = self._wallet.classic_address
        self.rpc = rpc
        self.max_session_loss = max_session_loss
        self.assets = cfg.assets()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._xrp = 0.0
        self._reserve = 0.0
        self._lines: dict[str, float] = {}  # asset key -> balance on our trust line
        self._refreshed = 0.0
        self._fee_drops = 0
        self._losses_in_a_row = 0
        self.refresh_account()
        self._check_setup()

    # -- account -----------------------------------------------------------------------------------------------------

    def refresh_account(self) -> None:
        try:
            info = self.rpc.call("account_info", account=self.address, ledger_index="validated")["account_data"]
        except RpcError as e:
            if e.code == "actNotFound":
                raise LiveSetupError(f"account {self.address} does not exist on the ledger (it needs its base reserve in XRP)")
            raise
        srv = self.rpc.call("server_info")["info"]["validated_ledger"]
        self._xrp = int(info["Balance"]) / 1e6
        self._reserve = srv["reserve_base_xrp"] + info.get("OwnerCount", 0) * srv["reserve_inc_xrp"]
        lines: dict[str, float] = {}
        for a in self.assets.values():
            if a.is_xrp:
                continue
            for ln in self.rpc.call("account_lines", account=self.address, peer=a.issuer, ledger_index="validated").get("lines", []):
                if ln["currency"] == a.currency:
                    lines[a.key] = float(ln["balance"])
        self._lines, self._refreshed = lines, time.time()
        base_balance = self._xrp if self.base.is_xrp else lines.get(self.base.key, 0.0)
        self.state.change(lambda: setattr(self.state, "account", {"address": self.address, "xrp": self._xrp, "base_balance": base_balance,
                                                                  "lines": {self.state.names.get(k, k): v for k, v in lines.items()}}))

    def _check_setup(self) -> None:
        if not self.base.is_xrp and self.base.key not in self._lines:
            raise LiveSetupError(f"the wallet has no trust line for the base asset {self.name(self.base)}")
        if self.spendable_base() < self.cfg.size_min:
            raise LiveSetupError(f"spendable {self.name(self.base)} ({self.spendable_base():.4f}) is below the smallest trade ({self.cfg.size_min})")

    def spendable_base(self) -> float:
        """Most base we can put into a cycle right now."""
        if time.time() - self._refreshed > STATE_REFRESH_S and not self._busy:
            try:
                self.refresh_account()
            except (RpcError, OSError):
                pass
        if self.base.is_xrp:
            return max(0.0, self._xrp - self._reserve - XRP_BUFFER)
        return max(0.0, self._lines.get(self.base.key, 0.0))

    def can_trade(self, route: Route) -> str | None:
        missing = [self.name(a) for a in route.assets if not a.is_xrp and a.key not in self._lines]
        if missing:
            return "no trust line for " + ", ".join(sorted(set(missing)))
        return None

    def selftest(self, routes: list[Route]) -> None:
        """Run the real transaction builder through the server's simulate once, so a wrong setup fails now and not mid-trade."""
        for route in routes:
            if self.can_trade(route):
                continue
            src, dst = route.assets[0], route.assets[1]
            spend = min(self.cfg.size_min, self.spendable_base())
            sim = self.rpc.call("simulate", tx_json=self.payment_json(src, dst, spend, 0.0, spend))
            if sim.get("engine_result") != "tesSUCCESS":
                raise LiveSetupError(f"self-test payment {self.name(src)}→{self.name(dst)} was not accepted by the server: {sim.get('engine_result')} {sim.get('engine_result_message', '')}")
            return
        raise LiveSetupError("no route can be traded from this wallet (missing trust lines?)")

    # -- trades ------------------------------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def open(self, ev: Evaluation, snap: Snapshot) -> int:
        with self._lock:
            if self._busy:
                raise RuntimeError("a trade is already in flight")
            self._busy = True
        tid = self._begin(ev, snap.ledger)
        self._thread = threading.Thread(target=self._run, args=(tid, ev, snap), daemon=True, name=f"trade-{tid}")
        self._thread.start()
        return tid

    def on_snapshot(self, snap: Snapshot) -> None:
        pass  # nothing to advance: a trade runs on its own thread

    def _run(self, tid: int, ev: Evaluation, snap: Snapshot) -> None:
        self._fee_drops = 0
        gen = run_plan(ev, self.cfg)
        try:
            attempt = next(gen)
            while True:
                self._progress(tid, f"leg {attempt.leg + 1} of {len(ev.legs)}: {attempt.kind}")
                attempt = gen.send(self._attempt(ev, attempt))
        except StopIteration as stop:
            out: Outcome = stop.value
        except Exception as e:  # unknown state mid-cycle: stop and let a person look at the wallet
            out = Outcome("STUCK", -ev.size * self.cfg.max_loss_bps / 1e4, f"unexpected error ({type(e).__name__}: {e}); check the wallet")
        try:
            fee_base = fee_in_base(snap, self.base, self.cfg, 1) / max(self.cfg.fee_drops, 1) * self._fee_drops
            realized = self._end(tid, out, fee_base, ev)
            try:
                self.refresh_account()
            except (RpcError, OSError) as e:
                self.state.log("warn", f"could not refresh balances: {e}")
            self._after_trade(out, realized)
        finally:
            self._busy = False  # whatever happened, never leave the executor stuck "busy"

    def _after_trade(self, out: Outcome, realized: float) -> None:
        self._losses_in_a_row = self._losses_in_a_row + 1 if out.status == "LOST" else 0
        if self._losses_in_a_row >= 3:
            self.state.halt("three losing trades in a row")
        elif self.max_session_loss is not None and self.state.stats["pnl_real"] <= -self.max_session_loss:
            self.state.halt(f"session loss limit reached ({self.state.stats['pnl_real']:+.4f} {self.name(self.base)})")

    # -- one payment -------------------------------------------------------------------------------------------------

    def payment_json(self, src: Asset, dst: Asset, spend: float, min_out: float, expected_out: float) -> dict:
        """Transaction fields common to simulation and submission: convert up to `spend` of src into at least `min_out` of dst."""
        tx = {"TransactionType": "Payment", "Account": self.address, "Destination": self.address, "Flags": TF_PARTIAL_PAYMENT,
              "SendMax": amount_json(src, spend), "Amount": amount_json(dst, max(expected_out, min_out) * CEILING)}
        if min_out > 0:
            tx["DeliverMin"] = amount_json(dst, min_out)
        return tx

    def _attempt(self, ev: Evaluation, a: Attempt) -> Result:
        src, dst = ev.route.assets[a.leg], ev.route.assets[a.leg + 1]
        planned = ev.legs[a.leg].amount_out
        tx = self.payment_json(src, dst, a.spend, a.min_out, planned)

        sim = self.rpc.call("simulate", tx_json=dict(tx))
        if sim.get("engine_result") != "tesSUCCESS":
            return Result(False, note=f"simulation says {sim.get('engine_result')}")
        delivered = sim.get("meta", {}).get("delivered_amount")
        if delivered is None:
            return Result(False, note="simulation reported no delivered amount")
        got = amount_value(delivered)
        if got < a.min_out * (1 - 1e-12):
            return Result(False, note=f"simulated fill is {got / planned - 1:+.2%} vs quote")

        fees = self.rpc.call("fee")["drops"]
        fee = max(int(fees["base_fee"]), int(fees["open_ledger_fee"]))
        if fee > MAX_FEE_DROPS:
            return Result(False, note=f"network fee {fee} drops is above the {MAX_FEE_DROPS} limit")
        acct = self.rpc.call("account_info", account=self.address, ledger_index="current")
        last_ledger = acct["ledger_current_index"] + LEDGERS_TO_LIVE
        tx.update(Fee=str(fee), Sequence=acct["account_data"]["Sequence"], LastLedgerSequence=last_ledger)

        from xrpl.models.transactions import Payment
        from xrpl.transaction import sign

        signed = sign(Payment.from_xrpl(tx), self._wallet)
        txid = signed.get_hash()
        r = self.rpc.call("submit", tx_blob=signed.blob())
        if not (r["engine_result"].startswith("tes") or r["engine_result"] == "terQUEUED"):
            return Result(False, note=f"submit refused: {r['engine_result']}")  # tem/tef/tel/ter: not applied, no fee
        return self._await(txid, last_ledger, fee)

    def _await(self, txid: str, last_ledger: int, fee: int) -> Result:
        """Follow a submitted transaction to a validated result."""
        while True:
            time.sleep(1.0)
            try:
                r = self.rpc.call("tx", transaction=txid)
            except RpcError as e:
                if e.code != "txnNotFound":
                    raise
                r = None
            if r and r.get("validated"):
                self._fee_drops += fee  # a validated transaction pays its fee whether or not the payment went through
                code = r["meta"]["TransactionResult"]
                if code == "tesSUCCESS":
                    return Result(True, amount_value(r["meta"]["delivered_amount"]), sent=True)
                return Result(False, sent=True, note=f"validated as {code}")
            if int(self.rpc.call("ledger", ledger_index="validated")["ledger"]["ledger_index"]) > last_ledger:
                try:  # the window has closed; one last look in case it made the final ledger
                    r = self.rpc.call("tx", transaction=txid)
                    if r.get("validated"):
                        continue
                except RpcError:
                    pass
                return Result(False, note="not validated before it expired; it can no longer be applied")
