"""The research harness has to be trustworthy before anything it says about trading is: test it against known truth."""

import gzip
import math
import os
import shutil
import tempfile
import unittest

import numpy as np

SNAPSHOT = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots", "raw_main_final.jsonl.gz")
try:
    import lightgbm  # noqa: F401
    import sklearn  # noqa: F401
    HAVE_ML = True
except ImportError:
    HAVE_ML = False

if HAVE_ML and os.path.exists(SNAPSHOT):
    from research import stats
    from research.data import Market, guard_path, load
    from research.lab import Lab, corrupt_future
    from research.ledger import Ledger
    from research.outcomes import POLICIES, net_return
    from research.registry import FEATURE_SETS, feature_set

NEEDS = unittest.skipUnless(HAVE_ML and os.path.exists(SNAPSHOT), "needs scikit-learn, lightgbm and the committed recording snapshot")
_tmp = None
_tokens = None


def tokens():
    global _tmp, _tokens
    if _tokens is None:
        _tmp = tempfile.mkdtemp()
        raw = os.path.join(_tmp, "raw.jsonl")
        with gzip.open(SNAPSHOT, "rb") as src, open(raw, "wb") as dst:
            shutil.copyfileobj(src, dst)
        _tokens = load([raw])
    return _tokens


def raw_path():
    tokens()
    return os.path.join(_tmp, "raw.jsonl")


def fresh_lab():
    d = tempfile.mkdtemp()
    return Lab(tokens=tokens(), ledger_dir=os.path.join(d, "ledger"), cache_dir=os.path.join(tempfile.gettempdir(), "research_test_cache"))


@NEEDS
class StatsAreCalibrated(unittest.TestCase):
    def sparse_noise(self, rng, n, k):
        sel = rng.random((n, k)) < np.array([0.05, 0.2, 0.5] * (k // 3 + 1))[:k]
        return np.where(sel, rng.normal(0, 12, size=(n, k)), 0.0), sel   # zero-mean returns on the launches each trial chose to trade

    def test_reality_check_is_not_fooled_by_the_best_of_many_noise_trials(self):
        rng = np.random.default_rng(1)
        hits = 0
        for _ in range(80):
            V, sel = self.sparse_noise(rng, 770, 24)
            hits += stats.reality_check(V, sel, seed=3, draws=500)["p"] < 0.05
        self.assertLessEqual(hits / 80, 0.10, hits)

    def test_reality_check_finds_a_real_edge_hidden_among_noise_trials(self):
        rng = np.random.default_rng(2)
        found = 0
        for _ in range(30):
            V, sel = self.sparse_noise(rng, 770, 24)
            pick = rng.random(770) < 0.3
            V[:, 7] = np.where(pick, rng.normal(10, 12, size=770), 0.0)   # one trial with a genuine +10 per trade
            sel[:, 7] = pick
            r = stats.reality_check(V, sel, seed=3, draws=500)
            found += r["p"] < 0.05 and r["best_index"] == 7
        self.assertGreaterEqual(found / 30, 0.9, found)

    def test_block_bootstrap_interval_covers_the_truth_about_as_often_as_claimed(self):
        rng = np.random.default_rng(3)
        cover = 0
        for i in range(120):
            sel = rng.random(300) < 0.3
            vals = np.where(sel, rng.normal(2.0, 12, size=300), 0.0)
            lo, hi = stats.ratio_ci(vals, sel, seed=i, draws=600)
            cover += lo <= 2.0 <= hi
        self.assertGreaterEqual(cover / 120, 0.88)

    def test_empty_and_degenerate_inputs(self):
        self.assertTrue(all(math.isnan(x) for x in stats.ratio_ci(np.zeros(10), np.zeros(10, dtype=bool))))
        self.assertEqual(stats.reality_check(np.zeros((50, 0)))["trials"], 0)
        self.assertEqual(stats.reality_check(np.zeros((50, 3)))["p"], 1.0)


@NEEDS
class NoPeeking(unittest.TestCase):
    def test_builtin_feature_sets_do_not_change_when_the_future_is_scrambled(self):
        lab = fresh_lab()
        for name in ("launch", "tape", "scan", "market", "creator"):
            for delay in (0.0, 15.0):
                lab.check_no_lookahead(name, delay, sample=25)

    def test_a_context_builder_that_leaks_the_future_is_caught(self):
        """If the machinery that hands a feature its data ever let the future through, the corrupted-future check must notice."""
        import research.lab as labmod
        from research.context import build_context as honest
        from dataclasses import replace

        def leaky(tok, market, delay):
            ctx = honest(tok, market, delay)
            return None if ctx is None else replace(ctx, obs=tuple(tok.obs))   # all observations, including those after the decision

        lab = fresh_lab()
        original = labmod.build_context
        labmod.build_context = leaky
        try:
            with self.assertRaises(ValueError) as cm:
                lab.check_no_lookahead("tape", 0.0, sample=30)
            self.assertIn("after the decision time", str(cm.exception))
        finally:
            labmod.build_context = original

    def test_plugins_that_could_reach_the_future_other_ways_are_rejected_before_they_run(self):
        from research.registry import load_plugins
        from research.sandbox import PluginRejected
        cheats = {
            "reads_the_file": "from research.registry import feature_set\n@feature_set('c1')\ndef f(c):\n    return {'x': len(open('data/raw_main.jsonl').read())}\n",
            "imports_the_loader": "from research.data import load\nfrom research.registry import feature_set\n@feature_set('c2')\ndef f(c):\n    return {'x': 1.0}\n",
            "imports_os": "import os\nfrom research.registry import feature_set\n@feature_set('c3')\ndef f(c):\n    return {'x': 1.0}\n",
            "loads_arrays": "import numpy as np\nfrom research.registry import feature_set\n@feature_set('c4')\ndef f(c):\n    return {'x': float(np.load('r.npy')[0])}\n",
            "digs_through_the_context": "from research.registry import feature_set\n@feature_set('c5')\ndef f(c):\n    return {'x': float(c.__class__ is None)}\n",
            "runs_code_on_import": "from research.registry import feature_set\nprint('hi')\n@feature_set('c6')\ndef f(c):\n    return {'x': 1.0}\n",
            "uses_getattr": "from research.registry import feature_set\n@feature_set('c7')\ndef f(c):\n    return {'x': getattr(c, 'price_now')}\n",
        }
        for name, source in cheats.items():
            d = tempfile.mkdtemp()
            with open(os.path.join(d, f"{name}.py"), "w") as fh:
                fh.write(source)
            with self.assertRaises(PluginRejected, msg=name):
                load_plugins(d)
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "fine.py"), "w") as fh:
            fh.write("import math\nfrom research.registry import feature_set\n@feature_set('fine_sandbox_test')\ndef f(c):\n    return {'x': math.log(1 + len(c.name))}\n")
        self.assertEqual(load_plugins(d), ["fine.py"])
        FEATURE_SETS.pop("fine_sandbox_test", None)

    def test_the_corrupted_world_really_differs_after_t_and_not_before(self):
        toks = tokens()
        t = toks[300].t_launch + 60_000
        bad = corrupt_future(toks, t, seed=1)
        changed_after = sum(1 for a, b in zip(toks, bad) if any(x != y for x, y in zip(a.obs, b.obs) if x[0] > t))
        same_before = all(x == y for a, b in zip(toks, bad) for x, y in zip(a.obs, b.obs) if x[0] <= t)
        self.assertGreater(changed_after, 50)
        self.assertTrue(same_before)
        self.assertEqual(Market(toks).view(t), Market(toks).view(t))

    def test_the_market_view_contains_nothing_from_after_the_moment(self):
        toks = tokens()
        m = Market(toks)
        t = toks[500].t_launch + 30_000
        for s in m.view(t):
            self.assertLessEqual(s.t_launch, t)
            self.assertTrue(all(o[0] <= t for o in s.obs))
            self.assertTrue(s.check is None or s.check[0] <= t)

    def test_sealed_recordings_cannot_be_opened_by_the_research_loader(self):
        for name in ("data/raw_run2.jsonl", "x/holdout_1.jsonl", "sealed.jsonl"):
            with self.assertRaises(PermissionError):
                guard_path(name)
        guard_path("data/raw_main.jsonl")
        guard_path("data/raw_run2.jsonl", unseal=True)


@NEEDS
class WalkForwardIsHonest(unittest.TestCase):
    def test_no_training_launch_has_an_outcome_that_overlaps_the_test_block(self):
        lab = fresh_lab()
        for policy in ("tp30sl15", "ladder", "hold300"):
            for delay in (0.0, 30.0):
                folds, block = lab.folds(policy, delay, 0.0)
                self.assertEqual(len(folds), 4)
                for train, test, j in folds:
                    start = min(lab.tokens[i].t_launch for i in np.where(block == j)[0])
                    hold = POLICIES[policy].get("max_hold_s", 600)
                    from research.outcomes import LATENCY_MS, decision_time
                    self.assertTrue(all(decision_time(lab.tokens[i], delay) + LATENCY_MS + hold * 1000 <= start for i in train))
                    self.assertTrue(set(train).isdisjoint(test))
                    self.assertTrue(all(block[i] < j for i in train))
                    self.assertTrue(all(block[i] == j for i in test))
                    self.assertGreater(len(train), 60)

    def test_only_later_blocks_are_ever_tested(self):
        lab = fresh_lab()
        e = lab.evaluate(["launch", "tape"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        per_launch, selected = lab.ledger.vectors(e["id"])
        _, block = lab.folds("tp30sl15", 0.0, 0.0)
        self.assertEqual(len(per_launch), int((block >= 2).sum()))
        self.assertEqual(e["summary"]["n_test_launches"], len(per_launch))


@NEEDS
class Outcomes(unittest.TestCase):
    def test_a_token_nobody_trades_after_the_quote_costs_about_two_fees_not_nothing(self):
        toks = tokens()
        flat = [net_return(t, "tp30sl15", 0.0, 0.0) for t in toks if t.end_reason == "idle" and len(t.obs) == 1]
        flat = [x for x in flat if not math.isnan(x)]
        self.assertGreater(len(flat), 100)
        self.assertAlmostEqual(float(np.median(flat)), -3.1, delta=0.4)

    def test_transaction_cost_lowers_returns(self):
        toks = tokens()[:300]
        pairs = [(net_return(t, "tp30sl15", 0.0, 0.0), net_return(t, "tp30sl15", 0.0, 0.003)) for t in toks]
        pairs = [(a, b) for a, b in pairs if not math.isnan(a)]
        self.assertGreater(len(pairs), 100)
        self.assertLess(np.mean([b for _, b in pairs]), np.mean([a for a, _ in pairs]) - 0.4)
        self.assertGreater(np.mean([b < a for a, b in pairs]), 0.95)   # (a take-profit can trigger a touch later with costs and overshoot: rare)

    def test_blind_entry_loses_under_every_policy(self):
        lab = fresh_lab()
        for policy in POLICIES:
            y = lab.outcomes.returns(policy, 0.0, 0.0)
            self.assertLess(np.nanmean(y), -2.0, policy)


@NEEDS
class TheHarnessFindsWhatIsThereAndOnlyThat(unittest.TestCase):
    def plant(self, lab, effect):
        """Real returns, plus `effect` percentage points for tokens whose name is long, which the 'launch' features can see."""
        y = lab.outcomes.returns("tp30sl15", 0.0, 0.0).copy()
        hidden = np.array([len(t.name or "") >= 9 for t in lab.tokens])
        y[hidden & ~np.isnan(y)] += effect
        lab.outcomes._mem[("tp30sl15", 0.0, 0.0)] = y
        return hidden

    def test_a_planted_edge_is_found_and_survives_the_reality_check(self):
        lab = fresh_lab()
        hidden = self.plant(lab, 40.0)
        self.assertGreater(hidden.sum(), 100)
        for model in ("ridge", "lgbm_reg"):
            for q in (0.1, 0.2):
                lab.evaluate(["launch"], model, 0.0, "tp30sl15", 0.0, q)
        for model in ("ridge", "lgbm_reg"):
            lab.evaluate(["tape", "market"], model, 0.0, "tp30sl15", 0.0, 0.1)
        v = lab.verdict(min_trades=20)
        self.assertTrue(v["passes_reality_check"], v)
        best = max(lab.ledger.entries(), key=lambda e: e["summary"]["ci_lo"])
        self.assertEqual(best["spec"]["features"], ["launch"])
        self.assertGreater(best["summary"]["mean_per_trade"], 10)

    def test_with_no_edge_the_best_of_many_trials_is_rarely_declared_a_winner(self):
        """Shuffle the real returns across launches (destroying any link to the features) and run a grid; count false alarms."""
        rng = np.random.default_rng(7)
        false_alarms = naive = 0
        reps = 25
        for r in range(reps):
            lab = fresh_lab()
            y = lab.outcomes.returns("tp30sl15", 0.0, 0.0).copy()
            ok = np.where(~np.isnan(y))[0]
            y[ok] = rng.permutation(y[ok])
            y[ok] -= y[ok].mean()   # a market where buying anything has exactly zero expected return (the real one is about -6%, where false alarms are impossible)
            lab.outcomes._mem[("tp30sl15", 0.0, 0.0)] = y
            for sets in (["launch"], ["tape"], ["market", "creator"], ["launch", "tape", "scan", "market", "creator"]):
                for model in ("ridge", "lgbm_reg", "lgbm_clf"):
                    for q in (0.05, 0.2):
                        lab.evaluate(sets, model, 0.0, "tp30sl15", 0.0, q, seed=r)
            v = lab.verdict(min_trades=30)
            false_alarms += bool(v["passes_reality_check"])
            naive += bool(v["ci_positive_unadjusted"])
        self.assertLessEqual(false_alarms / reps, 0.10, (false_alarms, naive))
        self.assertGreater(naive, false_alarms)   # without the correction the same grid would 'find' edges far more often
        self.assertGreaterEqual(naive, 1)


@NEEDS
class LedgerKeepsCount(unittest.TestCase):
    def test_repeating_a_trial_does_not_add_to_the_count_but_changing_the_seed_does(self):
        lab = fresh_lab()
        a = lab.evaluate(["launch"], "lgbm_reg", 0.0, "tp30sl15", 0.0, 0.2, seed=0)
        b = lab.evaluate(["launch"], "lgbm_reg", 0.0, "tp30sl15", 0.0, 0.2, seed=0)
        c = lab.evaluate(["launch"], "lgbm_reg", 0.0, "tp30sl15", 0.0, 0.2, seed=1)
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])
        self.assertEqual(len(lab.ledger.entries()), 2)

    def test_results_are_reproducible(self):
        a, b = fresh_lab(), fresh_lab()
        ra = a.evaluate(["launch", "tape"], "lgbm_clf", 0.0, "smart", 0.0, 0.1, seed=3)["summary"]
        rb = b.evaluate(["launch", "tape"], "lgbm_clf", 0.0, "smart", 0.0, 0.1, seed=3)["summary"]
        self.assertEqual(ra, rb)

    def test_bad_requests_are_refused(self):
        lab = fresh_lab()
        for kwargs in ({"model": "magic"}, {"policy": "yolo"}, {"q": 0.0}, {"q": 1.5}):
            with self.assertRaises(ValueError):
                lab.evaluate(["launch"], **{"model": "ridge", "policy": "tp30sl15", "q": 0.1, **kwargs})
        with self.assertRaises(ValueError):
            lab.evaluate(["no_such_set"], "ridge")


@NEEDS
class SealedConfirmation(unittest.TestCase):
    """Plumbing of the one-shot test on the sealed recording (here the 'sealed' file is a copy of the dev data, so the numbers mean nothing)."""

    def setUp(self):
        from research import confirm
        self.confirm = confirm
        self.d = tempfile.mkdtemp()
        self.saved = (confirm.FROZEN_DIR, confirm.LOG, confirm.MAX_SPECS)
        confirm.FROZEN_DIR, confirm.LOG, confirm.MAX_SPECS = os.path.join(self.d, "frozen"), os.path.join(self.d, "log.jsonl"), 2
        self.holdout = os.path.join(self.d, "holdout_copy.jsonl")
        shutil.copy(raw_path(), self.holdout)

    def tearDown(self):
        self.confirm.FROZEN_DIR, self.confirm.LOG, self.confirm.MAX_SPECS = self.saved

    def args(self, name, **kw):
        import argparse
        base = dict(data=raw_path(), holdout=self.holdout, name=name, features="launch,tape", model="ridge", policy="tp30sl15", delay=0.0, cost=0.0, q=0.2, seed=0, note="t", spec_file=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def freeze(self, name, **kw):
        a = self.args(name, **kw)
        a.spec_file = self.confirm.freeze(a)
        return a

    def test_freeze_then_run_once_and_the_result_is_logged(self):
        a = self.freeze("c1")
        r = self.confirm.run(a)
        self.assertGreater(r["n_selected"], 30)
        self.assertEqual(set(r) >= {"mean_per_trade", "ci_lo", "ci_hi", "p_mean_le_zero", "blind_per_trade", "bonferroni_alpha"}, True)
        again = self.confirm.run(a)
        self.assertEqual(again["time"], r["time"])
        self.assertEqual(len(self.confirm.log_entries()), 1)

    def test_a_frozen_spec_cannot_be_overwritten_and_only_a_few_specs_may_use_the_sealed_data(self):
        self.freeze("c1")
        with self.assertRaises(SystemExit):
            self.freeze("c1", q=0.3)
        for n in ("c1", "c2"):
            a = self.args(n)
            a.spec_file = os.path.join(self.confirm.FROZEN_DIR, f"{n}.json") if n == "c1" else self.confirm.freeze(a)
            self.confirm.run(a)
        a = self.freeze("c3", q=0.4)
        with self.assertRaises(SystemExit):
            self.confirm.run(a)

    def test_a_changed_harness_or_feature_file_voids_the_freeze(self):
        a = self.freeze("c1")
        saved = self.confirm.harness_hash
        self.confirm.harness_hash = lambda: "changed"
        try:
            with self.assertRaises(SystemExit):
                self.confirm.run(a)
        finally:
            self.confirm.harness_hash = saved
        import json
        with open(a.spec_file) as f:
            frozen = json.load(f)
        frozen["spec"]["set_hashes"]["launch"] = "deadbeef0000"
        with open(a.spec_file, "w") as f:
            json.dump(frozen, f)
        with self.assertRaises(SystemExit):
            self.confirm.run(a)

    def test_the_research_loader_refuses_the_sealed_name_but_confirm_can_open_it(self):
        with self.assertRaises(PermissionError):
            load([self.holdout])
        self.assertGreater(len(load([self.holdout], unseal=True)), 100)


if __name__ == "__main__":
    unittest.main()
