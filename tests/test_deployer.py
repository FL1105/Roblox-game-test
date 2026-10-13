import unittest

from sniper.listener.__main__ import make_scan_fn
from sniper.pump.store import DeployerProfile, LaunchStore
from sniper.scanner import Severity, Thresholds, Verdict, scan_token
from sniper.scanner.checks import check_deployer, check_launch
from sniper.scanner.scan import WALLET_LOOKUP_LIMIT, with_wallet_activity
from sniper.scanner.sources import SolanaRpc

from .test_scanner import MINT, FakeNetwork, severities

T = Thresholds()


class LaunchChecks(unittest.TestCase):
    def test_dev_buy_tiers(self):
        self.assertEqual(severities(check_launch(250_000_000, T)), {Severity.HIGH})    # 25%
        self.assertEqual(severities(check_launch(120_000_000, T)), {Severity.MEDIUM})  # 12%
        self.assertEqual(severities(check_launch(20_000_000, T)), {Severity.INFO})     # 2%
        self.assertEqual(severities(check_launch(0, T)), {Severity.INFO})


class DeployerChecks(unittest.TestCase):
    def test_serial_rugger_is_rejected(self):
        self.assertEqual(severities(check_deployer(DeployerProfile(rugs_total=2), T)), {Severity.CRITICAL})

    def test_single_prior_rug_is_high(self):
        self.assertEqual(severities(check_deployer(DeployerProfile(rugs_total=1), T)), {Severity.HIGH})

    def test_serial_launching_tiers(self):
        self.assertEqual(severities(check_deployer(DeployerProfile(launches_24h=6), T)), {Severity.HIGH})
        self.assertEqual(severities(check_deployer(DeployerProfile(launches_24h=2), T)), {Severity.MEDIUM})

    def test_brand_new_wallet_is_medium_but_lower_bound_age_is_not(self):
        self.assertEqual(severities(check_deployer(DeployerProfile(wallet_age_hours=0.2), T)), {Severity.MEDIUM})
        capped = DeployerProfile(wallet_age_hours=0.2, wallet_age_is_lower_bound=True)
        self.assertEqual(severities(check_deployer(capped, T)), {Severity.INFO})

    def test_no_history_is_info_and_says_so(self):
        (f,) = check_deployer(DeployerProfile(), T)
        self.assertEqual(f.severity, Severity.INFO)
        self.assertIn("limited", f.message)


class WalletActivity(unittest.TestCase):
    def rpc(self, block_times):
        def http(url, data=None, timeout=15):
            return {"result": [{"signature": str(i), "blockTime": b} for i, b in enumerate(block_times)]}
        return SolanaRpc("http://rpc", http)

    def test_young_wallet(self):
        p = with_wallet_activity(self.rpc([990, 900, 800]), "W", DeployerProfile(), now_s=1000)
        self.assertAlmostEqual(p.wallet_age_hours, 200 / 3600)
        self.assertFalse(p.wallet_age_is_lower_bound)
        self.assertEqual(p.wallet_tx_visible, 3)

    def test_full_page_means_age_is_only_a_lower_bound(self):
        p = with_wallet_activity(self.rpc([500] * WALLET_LOOKUP_LIMIT), "W", DeployerProfile(), now_s=1000)
        self.assertTrue(p.wallet_age_is_lower_bound)

    def test_wallet_with_no_signatures_leaves_profile_unchanged(self):
        base = DeployerProfile(launches_total=2)
        self.assertEqual(with_wallet_activity(self.rpc([]), "W", base, now_s=1000), base)


class ScanIntegration(unittest.TestCase):
    def scan(self, **kw):
        net = FakeNetwork()
        return scan_token(MINT, SolanaRpc("http://rpc", net), now_ms=10**12, **kw)

    def test_serial_rugger_rejects_otherwise_clean_token(self):
        result = self.scan(deployer=DeployerProfile(rugs_total=3), dev_tokens=0)
        self.assertEqual(result.verdict, Verdict.REJECT)

    def test_big_dev_buy_pushes_clean_token_to_avoid(self):
        result = self.scan(deployer=DeployerProfile(), dev_tokens=300_000_000)
        self.assertEqual(result.verdict, Verdict.AVOID)

    def test_clean_deployer_and_small_dev_buy_still_passes(self):
        result = self.scan(deployer=DeployerProfile(), dev_tokens=10_000_000)
        self.assertEqual(result.verdict, Verdict.PASS)


class FreshMintRetry(unittest.TestCase):
    """A just-created mint can be invisible to the RPC for a moment; don't call it fake on the first miss."""

    class Flaky(FakeNetwork):
        def __init__(self, misses):
            super().__init__()
            self.misses = misses

        def __call__(self, url, data=None, timeout=15):
            if data and data["method"] == "getAccountInfo" and self.misses > 0:
                self.misses -= 1
                return {"result": {"value": None}}
            return super().__call__(url, data, timeout)

    def launch(self):
        from sniper.pump.events import NewToken
        return NewToken(1, MINT, "CREATOR", "n", "S", 0.0, 0.0, 30.0, 1.073e9)

    def run_scan(self, misses):
        scan = make_scan_fn(SolanaRpc("http://rpc", self.Flaky(misses)), LaunchStore(), wallet_lookup=False, retry_delay_s=0)
        return scan(self.launch())

    def test_recovers_when_mint_appears_on_retry(self):
        verdict, _, _ = self.run_scan(misses=2)
        self.assertEqual(verdict, "PASS")

    def test_gives_up_after_retries(self):
        verdict, _, summary = self.run_scan(misses=99)
        self.assertEqual(verdict, "REJECT")
        self.assertIn("critical:mint", summary)


if __name__ == "__main__":
    unittest.main()
