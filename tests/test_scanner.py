import unittest

from sniper.scanner import Severity, Thresholds, Verdict, scan_token
from sniper.scanner.checks import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    check_holders,
    check_market,
    check_mint,
    check_roundtrip,
)
from sniper.scanner.models import Holder, RoundTrip
from sniper.scanner.sources import DexScreener, JupiterQuotes, SolanaRpc, SourceError

T = Thresholds()
MINT = "8b2Qv3xYk4Zr6s7t9uWc1dEfGhJkLmNpQrStUvWxYzAb"[:44]
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CURVE = "CurveOwner1111111111111111111111111111111111"
WALLET_A = "WalletA11111111111111111111111111111111111"
WALLET_B = "WalletB11111111111111111111111111111111111"


def mint_account(owner=TOKEN_PROGRAM, mint_auth=None, freeze_auth=None, extensions=None):
    info = {"mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "supply": "1000", "decimals": 6}
    if extensions is not None:
        info["extensions"] = extensions
    return {"owner": owner, "data": {"program": "spl-token", "parsed": {"type": "mint", "info": info}}}


def severities(findings):
    return {f.severity for f in findings}


class MintChecks(unittest.TestCase):
    def test_clean_mint_has_no_findings(self):
        self.assertEqual(check_mint(mint_account(), T), [])

    def test_missing_account_is_critical(self):
        self.assertEqual(severities(check_mint(None, T)), {Severity.CRITICAL})

    def test_non_token_program_owner_is_critical(self):
        acc = mint_account(owner="11111111111111111111111111111111")
        self.assertEqual(severities(check_mint(acc, T)), {Severity.CRITICAL})

    def test_authorities_are_critical(self):
        findings = check_mint(mint_account(mint_auth=WALLET_A, freeze_auth=WALLET_B), T)
        self.assertEqual({f.check for f in findings}, {"mint_authority", "freeze_authority"})
        self.assertEqual(severities(findings), {Severity.CRITICAL})

    def ext(self, name, **state):
        return mint_account(owner=TOKEN_2022_PROGRAM, extensions=[{"extension": name, "state": state}])

    def test_permanent_delegate_is_critical(self):
        self.assertEqual(severities(check_mint(self.ext("permanentDelegate", delegate=WALLET_A), T)), {Severity.CRITICAL})

    def test_non_transferable_is_critical(self):
        self.assertEqual(severities(check_mint(self.ext("nonTransferable"), T)), {Severity.CRITICAL})

    def test_default_frozen_is_critical_but_initialized_is_fine(self):
        self.assertEqual(severities(check_mint(self.ext("defaultAccountState", accountState="frozen"), T)), {Severity.CRITICAL})
        self.assertEqual(check_mint(self.ext("defaultAccountState", accountState="initialized"), T), [])

    def test_transfer_hook_high_only_when_program_set(self):
        self.assertEqual(severities(check_mint(self.ext("transferHook", programId=WALLET_A), T)), {Severity.HIGH})
        self.assertEqual(check_mint(self.ext("transferHook", programId=None), T), [])

    def test_transfer_fee_tiers(self):
        def fee(bps, authority=None):
            return self.ext(
                "transferFeeConfig",
                newerTransferFee={"transferFeeBasisPoints": bps},
                olderTransferFee={"transferFeeBasisPoints": 0},
                transferFeeConfigAuthority=authority,
            )

        self.assertEqual(severities(check_mint(fee(3000), T)), {Severity.CRITICAL})
        self.assertEqual(severities(check_mint(fee(500), T)), {Severity.HIGH})
        self.assertEqual(severities(check_mint(fee(100), T)), {Severity.MEDIUM})
        self.assertEqual(severities(check_mint(fee(0, authority=WALLET_A), T)), {Severity.MEDIUM})
        self.assertEqual(check_mint(fee(0), T), [])


class HolderChecks(unittest.TestCase):
    def test_pool_and_curve_are_excluded_from_concentration(self):
        holders = [Holder(CURVE, 800, "bonding_curve")] + [Holder(f"w{i}", 20) for i in range(10)]
        findings = check_holders(holders, 1000, T)
        # Each wallet holds 10% of the 200 circulating; top-10 hold 100% of circulating.
        self.assertIn(Severity.HIGH, severities(findings))
        self.assertNotIn("top_holder", {f.check for f in findings})

    def test_dominant_wallet_is_high(self):
        findings = check_holders([Holder(WALLET_A, 500), Holder(WALLET_B, 10)], 1000, T)
        self.assertIn(("top_holder", Severity.HIGH), {(f.check, f.severity) for f in findings})

    def test_distributed_supply_is_clean(self):
        holders = [Holder(f"w{i}", 10) for i in range(20)]  # 20 wallets x 1% each of 1000
        self.assertEqual(severities(check_holders(holders, 1000, T)), {Severity.INFO})

    def test_same_owner_accounts_are_aggregated(self):
        holders = [Holder(WALLET_A, 100), Holder(WALLET_A, 100)] + [Holder(f"w{i}", 1) for i in range(10)]
        findings = check_holders(holders, 1000, T)
        self.assertIn("top_holder", {f.check for f in findings})  # 200/1000 -> 20% ~ medium+


class MarketChecks(unittest.TestCase):
    NOW = 1_700_000_000_000

    def pair(self, liq=50_000, age_min=120, buys=10, sells=8):
        return {
            "liquidity": {"usd": liq},
            "pairCreatedAt": self.NOW - age_min * 60_000,
            "txns": {"h1": {"buys": buys, "sells": sells}},
        }

    def test_no_pairs_is_info(self):
        self.assertEqual(severities(check_market([], self.NOW, T)), {Severity.INFO})

    def test_healthy_pair_has_no_warnings(self):
        self.assertEqual(severities(check_market([self.pair()], self.NOW, T)), {Severity.INFO})

    def test_thin_liquidity(self):
        self.assertIn(Severity.HIGH, severities(check_market([self.pair(liq=2_000)], self.NOW, T)))
        self.assertIn(Severity.MEDIUM, severities(check_market([self.pair(liq=10_000)], self.NOW, T)))

    def test_buys_with_no_sells_is_flagged(self):
        findings = check_market([self.pair(buys=40, sells=0)], self.NOW, T)
        self.assertIn(("sell_activity", Severity.HIGH), {(f.check, f.severity) for f in findings})

    def test_uses_deepest_pair(self):
        findings = check_market([self.pair(liq=100), self.pair(liq=90_000)], self.NOW, T)
        self.assertNotIn(Severity.HIGH, severities(findings))

    def test_young_pair(self):
        self.assertIn("age", {f.check for f in check_market([self.pair(age_min=2)], self.NOW, T)})


class RoundTripChecks(unittest.TestCase):
    def test_no_buy_route_is_inconclusive(self):
        self.assertEqual(severities(check_roundtrip(RoundTrip(100, None, None, buy_error="NO_ROUTE"), T)), {Severity.INFO})

    def test_buy_route_without_sell_route_is_critical(self):
        self.assertEqual(severities(check_roundtrip(RoundTrip(100, 5000, None, sell_error="NO_ROUTE"), T)), {Severity.CRITICAL})

    def test_loss_tiers(self):
        def sev(back):
            return severities(check_roundtrip(RoundTrip(100, 5000, back), T))

        self.assertEqual(sev(60), {Severity.CRITICAL})
        self.assertEqual(sev(85), {Severity.HIGH})
        self.assertEqual(sev(93), {Severity.MEDIUM})
        self.assertEqual(sev(98), {Severity.INFO})


class FakeNetwork:
    """Routes calls like the real services and records them. Responses mimic real payload shapes."""

    def __init__(self, mint_acc=mint_account(), fail=()):
        self.mint_acc = mint_acc
        self.fail = set(fail)
        self.calls = []

    def token_account(self, owner):
        return {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "account", "info": {"owner": owner}}}}

    def __call__(self, url, data=None, timeout=15):
        self.calls.append(url if data is None else data["method"])
        if "dexscreener" in url:
            if "market" in self.fail:
                raise SourceError("down")
            return {"pairs": [{"liquidity": {"usd": 80_000}, "pairCreatedAt": 0, "txns": {"h1": {"buys": 10, "sells": 9}}}]}
        if "jup.ag" in url:
            if "inputMint=So111" in url:
                return {"outAmount": "5000"}
            return {"outAmount": "99000000"}
        method = data["method"]
        if "rpc" in self.fail:
            raise SourceError("rpc down")
        if method == "getAccountInfo":
            return {"result": {"value": self.mint_acc}}
        if method == "getTokenSupply":
            return {"result": {"value": {"amount": "1000"}}}
        if method == "getTokenLargestAccounts":
            return {"result": {"value": [
                {"address": "curveTA", "amount": "800"},
                {"address": "aTA", "amount": "15"},
                {"address": "bTA", "amount": "10"},
            ]}}
        if method == "getMultipleAccounts":
            keys = data["params"][0]
            if keys[0] == "curveTA":
                return {"result": {"value": [self.token_account(CURVE), self.token_account(WALLET_A), self.token_account(WALLET_B)]}}
            table = {CURVE: {"owner": PUMP_PROGRAM}, WALLET_A: None, WALLET_B: None}
            return {"result": {"value": [table[k] for k in keys]}}
        raise AssertionError(method)


class EndToEnd(unittest.TestCase):
    def scan(self, net, **kw):
        return scan_token(MINT, SolanaRpc("http://rpc", net), DexScreener(net), JupiterQuotes(net), now_ms=10**12, **kw)

    def test_clean_pump_fun_style_token_passes_and_curve_is_excluded(self):
        net = FakeNetwork()
        result = self.scan(net)
        self.assertEqual(result.verdict, Verdict.PASS, result.findings)
        holders = next(f for f in result.findings if f.check == "holders")
        self.assertIn("80% of supply sits in pools/curve", holders.message)
        self.assertTrue(all(result.sources_ok.values()))

    def test_live_mint_authority_rejects(self):
        result = self.scan(FakeNetwork(mint_acc=mint_account(mint_auth=WALLET_A)))
        self.assertEqual(result.verdict, Verdict.REJECT)

    def test_missing_mint_short_circuits(self):
        net = FakeNetwork(mint_acc=None)
        result = self.scan(net)
        self.assertEqual(result.verdict, Verdict.REJECT)
        self.assertNotIn("getTokenLargestAccounts", net.calls)

    def test_rpc_failure_is_unknown_never_pass(self):
        result = self.scan(FakeNetwork(fail={"rpc"}))
        self.assertEqual(result.verdict, Verdict.UNKNOWN)
        self.assertFalse(result.sources_ok["mint"])

    def test_market_failure_is_recorded_but_does_not_hide_other_results(self):
        result = self.scan(FakeNetwork(fail={"market"}))
        self.assertFalse(result.sources_ok["market"])
        self.assertTrue(result.sources_ok["holders"])
        self.assertIn(Severity.MEDIUM, severities(result.findings))

    def test_invalid_address_is_rejected_before_any_call(self):
        net = FakeNetwork()
        with self.assertRaises(ValueError):
            scan_token("not-an-address", SolanaRpc("http://rpc", net))
        self.assertEqual(net.calls, [])

    def test_result_serialises(self):
        d = self.scan(FakeNetwork()).to_dict()
        self.assertEqual(d["verdict"], "PASS")
        self.assertEqual(d["mint"], MINT)


if __name__ == "__main__":
    unittest.main()
