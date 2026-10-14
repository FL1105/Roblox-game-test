"""The research harness has to be trustworthy before anything it says about trading is: test it against known truth."""

import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

SNAPSHOT = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots", "raw_main_final.jsonl.gz")
ROOT = os.path.join(os.path.dirname(__file__), "..")
try:
    import lightgbm  # noqa: F401
    import sklearn  # noqa: F401
    HAVE_ML = True
except ImportError:
    HAVE_ML = False

if HAVE_ML and os.path.exists(SNAPSHOT):
    from research import stats, worker
    from research.data import Market, guard_path, load
    from research.lab import DataChanged, Lab
    from research.ledger import Ledger
    from research.outcomes import POLICIES, net_return
    from research.registry import FEATURE_SETS
    from research.sandbox import PluginRejected, check_source

NEEDS = unittest.skipUnless(HAVE_ML and os.path.exists(SNAPSHOT), "needs scikit-learn, lightgbm and the committed recording snapshot")
CACHE = os.path.join(tempfile.gettempdir(), "research_test_cache")   # shared between runs: keyed by data and code, so it can never go stale
_tmp = None
_tokens = None


def raw_path():
    global _tmp
    if _tmp is None:
        _tmp = tempfile.mkdtemp()
        with gzip.open(SNAPSHOT, "rb") as src, open(os.path.join(_tmp, "raw.jsonl"), "wb") as dst:
            shutil.copyfileobj(src, dst)
    return os.path.join(_tmp, "raw.jsonl")


def read_rows():
    with open(raw_path()) as f:
        return [json.loads(line) for line in f if line.strip()]


def tokens():
    global _tokens
    if _tokens is None:
        _tokens = load([raw_path()])
    return _tokens


def fresh_lab(ledger=None, paths=None):
    d = tempfile.mkdtemp()
    return Lab(paths or [raw_path()], ledger_dir=ledger or os.path.join(d, "ledger"), cache_dir=CACHE)


def plugin_dir(**sources):
    d = tempfile.mkdtemp()
    for name, src in sources.items():
        with open(os.path.join(d, f"{name}.py"), "w") as f:
            f.write(src)
    return d


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

    def test_a_noisy_high_variance_trial_does_not_hide_a_real_edge_when_scaled_by_sigma(self):
        rng = np.random.default_rng(4)
        found = 0
        for _ in range(30):
            V = np.zeros((770, 6))
            sel = np.zeros((770, 6), dtype=bool)
            for j in range(5):   # five huge-variance (sigma 60) zero-mean trials
                pick = rng.random(770) < 0.3
                V[:, j] = np.where(pick, rng.normal(0, 60, 770), 0)
                sel[:, j] = pick
            pick = rng.random(770) < 0.3
            V[:, 5] = np.where(pick, rng.normal(6, 10, 770), 0)   # a real edge with sigma 10
            sel[:, 5] = pick
            sigma = np.array([60, 60, 60, 60, 60, 10.0])
            found += stats.reality_check(V, sel, seed=3, draws=500, sigma=sigma)["p"] < 0.05
        self.assertGreaterEqual(found / 30, 0.8, found)

    def test_p_value_is_never_zero_and_trials_with_few_trades_are_ignored(self):
        V = np.zeros((300, 2)); sel = np.zeros((300, 2), dtype=bool)
        V[:10, 0] = 50.0; sel[:10, 0] = True       # ten huge wins: far too few trades to count
        r = stats.reality_check(V, sel, seed=1, draws=200, min_trades=30)
        self.assertEqual((r["live_trials"], r["p"]), (0, 1.0))
        V[:, 1] = np.where(np.arange(300) % 2 == 0, 40.0, 0.0); sel[:, 1] = np.arange(300) % 2 == 0
        r = stats.reality_check(V, sel, seed=1, draws=200, min_trades=30)
        self.assertGreaterEqual(r["p"], 1 / 201)

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
class PluginsCannotCarryTheFuture(unittest.TestCase):
    CHEATS = {
        "mutable_default": "from research.registry import feature_set\n@feature_set('x1')\ndef f(c, _seen={}):\n    return {'x': 1.0}\n",
        "module_dict": "from research.registry import feature_set\n_S = {}\n@feature_set('x2')\ndef f(c):\n    return {'x': 1.0}\n",
        "module_list": "from research.registry import feature_set\n_S = []\n@feature_set('x3')\ndef f(c):\n    return {'x': 1.0}\n",
        "function_attribute": "from research.registry import feature_set\n@feature_set('x4')\ndef f(c):\n    f.memo = 1\n    return {'x': 1.0}\n",
        "answer_key": "from research.registry import feature_set\nKEY = (('GxGEw3GEnxJ5wixZXbfkkmhjshBstHD6qhWreoWKuC9Y', 1.0),)\n@feature_set('x5')\ndef f(c):\n    return {'x': 1.0}\n",
        "nested_function": "from research.registry import feature_set\n@feature_set('x6')\ndef f(c):\n    def g(v):\n        return v\n    return {'x': g(1.0)}\n",
        "two_parameters": "from research.registry import feature_set\n@feature_set('x7')\ndef f(c, d):\n    return {'x': 1.0}\n",
        "random_numbers": "import numpy as np\nfrom research.registry import feature_set\n@feature_set('x8')\ndef f(c):\n    return {'x': float(np.random.rand())}\n",
        "cache_decorator": "import functools\nfrom research.registry import feature_set\n@functools.lru_cache\n@feature_set('x9')\ndef f(c):\n    return {'x': 1.0}\n",
        "reads_a_file": "from research.registry import feature_set\n@feature_set('y1')\ndef f(c):\n    return {'x': float(len(open('data/raw_main.jsonl').read()))}\n",
        "imports_the_loader": "from research.data import load\nfrom research.registry import feature_set\n@feature_set('y2')\ndef f(c):\n    return {'x': 1.0}\n",
        "imports_os": "import os\nfrom research.registry import feature_set\n@feature_set('y3')\ndef f(c):\n    return {'x': 1.0}\n",
        "loads_arrays": "import numpy as np\nfrom research.registry import feature_set\n@feature_set('y4')\ndef f(c):\n    return {'x': float(np.load('r.npy')[0])}\n",
        "digs_through_objects": "from research.registry import feature_set\n@feature_set('y5')\ndef f(c):\n    return {'x': float(c.__class__ is None)}\n",
        "runs_code_on_import": "from research.registry import feature_set\nprint('hi')\n@feature_set('y6')\ndef f(c):\n    return {'x': 1.0}\n",
        "uses_getattr": "from research.registry import feature_set\n@feature_set('y7')\ndef f(c):\n    return {'x': getattr(c, 'price_now')}\n",
    }

    def test_every_known_way_of_keeping_state_or_reaching_outside_is_rejected_before_it_runs(self):
        for name, source in self.CHEATS.items():
            with self.subTest(name):
                with self.assertRaises(PluginRejected):
                    check_source(source, name)

    def test_a_broken_or_rejected_plugin_does_not_take_the_others_down(self):
        from research.registry import PLUGIN_ERRORS, load_plugins
        d = plugin_dir(bad=self.CHEATS["reads_a_file"], half_written="from research.registry import feature_set\n@feature_set('fine_one')\ndef f(c:\n",
                       fine="import math\nfrom research.registry import feature_set\n@feature_set('fine_sandbox_test')\ndef f(c):\n    return {'x': math.log(1 + len(c.name))}\n")
        self.assertEqual(load_plugins(d), ["fine.py"])
        self.assertEqual(set(PLUGIN_ERRORS) & {"bad.py", "half_written.py"}, {"bad.py", "half_written.py"})
        FEATURE_SETS.pop("fine_sandbox_test", None)

    def run_worker(self, plugins, name, delay=0.0):
        out = os.path.join(tempfile.mkdtemp(), "m.npz")
        r = subprocess.run([sys.executable, "-m", "research.worker", "--data", raw_path(), "--set", name, "--delay", str(delay), "--out", out, "--plugins-dir", plugins],
                           cwd=ROOT, capture_output=True, text=True, timeout=600, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return r, out

    def test_a_rejected_plugin_is_unavailable_to_the_worker(self):
        d = plugin_dir(sneaky=self.CHEATS["reads_a_file"].replace("'y1'", "'sneaky'"))
        r, out = self.run_worker(d, "sneaky")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not available", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_the_worker_builds_an_honest_plugin_and_records_what_it_checked(self):
        d = plugin_dir(honest="import math\nfrom research.registry import feature_set\n@feature_set('honest_logname')\ndef f(c):\n    return {'n': math.log(1 + len(c.name)), 'age': (c.t0 - c.t_launch) / 1000.0}\n")
        r, out = self.run_worker(d, "honest_logname")
        self.assertEqual(r.returncode, 0, r.stderr)
        z = np.load(out)
        self.assertEqual(list(z["names"]), ["age", "n"])
        self.assertGreaterEqual(int(z["checked"]), 100)

    def test_a_plugin_that_peeks_through_a_global_the_sandbox_cannot_see_is_caught_by_behaviour(self):
        """Built in-process (bypassing the static rules) to prove the dynamic checks work on their own: state kept between calls."""
        toks = tokens()
        state = {}

        def keyed_leak(c):
            # remember every context's market, then report the focal token's LATEST price seen in any remembered market
            state[c.mint] = c.market
            latest = c.price_now
            for market in state.values():
                for s in market:
                    if s.mint == c.mint and s.obs:
                        latest = s.obs[-1][1] / s.obs[-1][2]
            return {"future_ratio": math.log(latest / c.price_now)}

        with self.assertRaises(worker.Violation) as cm:
            worker.matrix(toks, "keyed_leak", 0.0, fn=keyed_leak)
        self.assertIn("pure function", str(cm.exception))

    def test_order_dependent_state_is_caught(self):
        toks = tokens()
        seen = []

        def counter(c):
            seen.append(1)
            return {"calls_so_far": float(len(seen))}

        with self.assertRaises(worker.Violation):
            worker.matrix(toks, "counter", 0.0, fn=counter)

    def test_a_feature_that_changes_when_the_future_is_scrambled_or_deleted_is_caught(self):
        toks = tokens()
        by_mint = {t.mint: t for t in toks}

        def peeks_values(c):   # uses a global (the real dataset) that no corruption can reach, so only the purity check can see it... and values stay stable
            return {"x": 1.0}

        def uses_count_of_later_tokens(c):
            return {"later": float(len([1 for s in c.market if s.t_launch > c.t0]))}   # always 0 in a correct Context: fine

        worker.matrix(toks, "ok1", 0.0, fn=peeks_values)
        worker.matrix(toks, "ok2", 0.0, fn=uses_count_of_later_tokens)

        import research.worker as w
        from dataclasses import replace
        from research.context import build_context as honest

        def leaky_builder(tok, market, delay):
            ctx = honest(tok, market, delay)
            return None if ctx is None else replace(ctx, obs=tuple(tok.obs))   # every quote, including those after the decision

        def uses_last_obs_time(c):
            return {"last_obs_age": float(c.obs[-1][0] - c.t0)}

        saved = w.build_context
        w.build_context = leaky_builder
        try:
            with self.assertRaises(worker.Violation):
                worker.matrix(toks, "uses_last_obs_time", 0.0, fn=uses_last_obs_time)
        finally:
            w.build_context = saved
        self.assertTrue(by_mint)

    def test_the_context_for_a_token_is_identical_in_the_world_with_its_future_deleted(self):
        toks = tokens()
        for i in (50, 400, 800, 1000):
            tok = toks[i]
            t0 = (tok.obs[0][0] if tok.obs else tok.t_launch) + 5_000
            from research.context import build_context
            real = build_context(tok, Market(toks), 5.0)
            if real is None:
                continue
            cut = worker.truncate(toks, real.t0)
            import dataclasses
            self.assertEqual(dataclasses.asdict(real), dataclasses.asdict(worker.context_of(cut, tok.idx, 5.0)))

    def test_builtin_feature_sets_pass_every_check_at_two_delays(self):
        toks = tokens()
        for name in ("launch", "tape", "scan", "market", "creator"):
            for delay in (0.0, 15.0):
                with self.subTest(name=name, delay=delay):
                    X, names, info = worker.matrix(toks, name, delay)
                    self.assertEqual(X.shape[0], len(toks))
                    self.assertGreater(info["checked"], 100)

    def test_the_market_view_contains_nothing_from_after_the_moment(self):
        toks = tokens()
        m = Market(toks)
        t = toks[500].t_launch + 30_000
        for s in m.view(t):
            self.assertLessEqual(s.t_launch, t)
            self.assertTrue(all(o[0] <= t for o in s.obs))
            self.assertTrue(s.check is None or s.check[0] <= t)


@NEEDS
class SealedRecordingsStaySealed(unittest.TestCase):
    def test_the_research_loader_refuses_sealed_names_copies_and_links(self):
        for name in ("data/raw_run2.jsonl", "x/holdout_1.jsonl", "sealed.jsonl"):
            with self.assertRaises(PermissionError):
                guard_path(name)
        guard_path("data/raw_main.jsonl")
        guard_path("data/raw_run2.jsonl", unseal=True)
        run2b = os.path.join(ROOT, "data", "raw_run2b.jsonl")
        if os.path.exists(run2b):
            d = tempfile.mkdtemp()
            copy = os.path.join(d, "innocent_name.jsonl")
            shutil.copy(run2b, copy)
            with self.assertRaises(PermissionError):
                guard_path(copy)
            link = os.path.join(d, "harmless.jsonl")
            os.symlink(run2b, link)
            with self.assertRaises(PermissionError):
                guard_path(link)


@NEEDS
class WalkForwardIsHonest(unittest.TestCase):
    def test_no_training_launch_has_an_outcome_that_overlaps_the_test_block(self):
        lab = fresh_lab()
        from research.outcomes import LATENCY_MS, decision_time
        for policy in ("tp30sl15", "ladder", "hold300"):
            for delay in (0.0, 30.0):
                folds, block = lab.folds(policy, delay, 0.0)
                self.assertEqual(len(folds), 4)
                for train, test, j in folds:
                    start = min(lab.tokens[i].t_launch for i in np.where(block == j)[0])
                    hold = POLICIES[policy].get("max_hold_s", 600)
                    self.assertTrue(all(decision_time(lab.tokens[i], delay) + LATENCY_MS + hold * 1000 <= start for i in train))
                    self.assertTrue(set(train).isdisjoint(test))
                    self.assertTrue(all(block[i] < j for i in train))
                    self.assertTrue(all(block[i] == j for i in test))
                    self.assertGreater(len(train), 60)

    def test_only_later_blocks_are_ever_tested(self):
        lab = fresh_lab()
        e = lab.evaluate(["launch", "tape"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        per_launch, _ = lab.ledger.vectors(e["id"])
        _, block = lab.folds("tp30sl15", 0.0, 0.0)
        self.assertEqual(len(per_launch), int((block >= 2).sum()))
        self.assertEqual(e["summary"]["n_test_launches"], len(per_launch))


@NEEDS
class TheEvaluationUniverseDoesNotDependOnWhatTokensDid(unittest.TestCase):
    def test_the_same_launches_are_in_every_policy_and_only_the_recording_tail_and_unquoted_tokens_are_out(self):
        lab = fresh_lab()
        masks = {p: np.isnan(lab.outcomes.returns(p, 0.0, 0.0)) for p in POLICIES}
        first = next(iter(masks.values()))
        for p, m in masks.items():
            self.assertTrue(np.array_equal(m, first), p)          # one pool for every policy: it cannot be defined by an exit rule's outcome
        toks = lab.tokens
        for i, t in enumerate(toks):
            expected_out = t.tail or not t.obs or t.obs[0][2] <= 280_000_000
            self.assertEqual(bool(first[i]), expected_out, t.mint)
        self.assertGreater(sum(t.tail for t in toks), 20)
        self.assertGreater(int((~first).sum()), 800)

    def test_no_hidden_engine_gate_drops_quiet_or_below_launch_price_tokens(self):
        lab = fresh_lab()
        below = [t for t in lab.tokens if t.obs and t.obs[0][1] < t.v_sol0 and not t.tail and t.obs[0][2] > 280_000_000]
        self.assertGreater(len(below), 20)   # tokens already trading below their launch price exist...
        y = lab.outcomes.returns("tp30sl15", 0.0, 0.0)
        self.assertTrue(all(not np.isnan(y[t.idx]) for t in below))   # ...and have an outcome

    def test_a_tradeable_token_that_makes_no_trade_is_an_error_not_a_silent_gap(self):
        from dataclasses import replace
        import research.outcomes as o
        tok = next(t for t in tokens() if not np.isnan(net_return(t, "tp30sl15", 0.0, 0.0)))
        saved = o.BASE
        o.BASE = replace(saved, max_dev_buy_pct=-1.0)   # a gate that rejects everything
        try:
            with self.assertRaises(RuntimeError):
                net_return(tok, "tp30sl15", 0.0, 0.0)
        finally:
            o.BASE = saved

    def test_two_launches_of_one_mint_are_dropped_rather_than_merged(self):
        toks = tokens()
        self.assertEqual(len({t.mint for t in toks}), len(toks))
        self.assertTrue(all(not t.obs or t.obs[0][0] >= t.t_launch for t in toks))


@NEEDS
class Outcomes(unittest.TestCase):
    def test_a_token_nobody_trades_after_the_quote_costs_about_two_fees_not_nothing(self):
        flat = [net_return(t, "tp30sl15", 0.0, 0.0) for t in tokens() if t.end_reason == "idle" and len(t.obs) == 1]
        flat = [x for x in flat if not math.isnan(x)]
        self.assertGreater(len(flat), 100)
        self.assertAlmostEqual(float(np.median(flat)), -3.1, delta=0.4)

    def test_transaction_cost_lowers_returns(self):
        pairs = [(net_return(t, "tp30sl15", 0.0, 0.0), net_return(t, "tp30sl15", 0.0, 0.003)) for t in tokens()[:300]]
        pairs = [(a, b) for a, b in pairs if not math.isnan(a)]
        self.assertGreater(len(pairs), 100)
        self.assertLess(np.mean([b for _, b in pairs]), np.mean([a for a, _ in pairs]) - 0.4)
        self.assertGreater(np.mean([b < a for a, b in pairs]), 0.95)   # (a take-profit can trigger a touch later with costs and overshoot: rare)

    def test_blind_entry_loses_under_every_policy(self):
        lab = fresh_lab()
        for policy in POLICIES:
            self.assertLess(np.nanmean(lab.outcomes.returns(policy, 0.0, 0.0)), -2.0, policy)

    def test_negative_costs_are_refused(self):
        with self.assertRaises(ValueError):
            fresh_lab().evaluate(["launch"], "ridge", 0.0, "tp30sl15", -0.01, 0.1)


@NEEDS
class TheHarnessFindsWhatIsThereAndOnlyThat(unittest.TestCase):
    def plant(self, lab, effect):
        """Real returns, plus `effect` percentage points for tokens whose name is long, which the 'launch' features can see."""
        y = lab.outcomes.returns("tp30sl15", 0.0, 0.001).copy()
        hidden = np.array([len(t.name or "") >= 9 for t in lab.tokens])
        y[hidden & ~np.isnan(y)] += effect
        lab.outcomes._mem[("tp30sl15", 0.0, 0.001)] = y
        return hidden

    def test_a_planted_edge_is_found_and_survives_the_reality_check(self):
        lab = fresh_lab()
        hidden = self.plant(lab, 40.0)
        self.assertGreater(hidden.sum(), 100)
        for model in ("ridge", "lgbm_reg"):
            for q in (0.2, 0.3):
                lab.evaluate(["launch"], model, 0.0, "tp30sl15", 0.001, q)
        for model in ("ridge", "lgbm_reg"):
            lab.evaluate(["tape", "market"], model, 0.0, "tp30sl15", 0.001, 0.2)
        v = lab.verdict(min_trades=40)
        self.assertTrue(v["passes_reality_check"], {k: v[k] for k in v if k not in ("best", "champion")})
        self.assertEqual(v["champion"]["spec"]["features"], ["launch"])
        self.assertGreater(v["champion"]["summary"]["mean_per_trade"], 10)

    def test_with_no_edge_the_best_of_many_trials_is_rarely_declared_a_winner(self):
        """Shuffle the real returns across launches (destroying any link to the features), centre them at zero, and run a grid; count false alarms."""
        rng = np.random.default_rng(7)
        false_alarms = naive = 0
        reps = 16
        for r in range(reps):
            lab = fresh_lab()
            y = lab.outcomes.returns("tp30sl15", 0.0, 0.001).copy()
            ok = np.where(~np.isnan(y))[0]
            y[ok] = rng.permutation(y[ok])
            y[ok] -= y[ok].mean()   # a market where buying anything has exactly zero expected return
            lab.outcomes._mem[("tp30sl15", 0.0, 0.001)] = y
            for sets in (["launch"], ["tape"], ["market", "creator"], ["launch", "tape", "scan", "market", "creator"]):
                for model in ("ridge", "lgbm_reg", "lgbm_clf"):
                    for q in (0.2, 0.3):
                        lab.evaluate(sets, model, 0.0, "tp30sl15", 0.001, q, seed=r)
            v = lab.verdict(min_trades=60)
            false_alarms += bool(v["passes_reality_check"])
            naive += bool(v["qualified"])
        self.assertLessEqual(false_alarms / reps, 0.10, (false_alarms, naive))
        self.assertGreaterEqual(naive, false_alarms)

    def test_a_result_that_only_works_for_free_cannot_pass(self):
        lab = fresh_lab()
        y = lab.outcomes.returns("tp30sl15", 0.0, 0.0).copy()
        hidden = np.array([len(t.name or "") >= 9 for t in lab.tokens])
        y[hidden & ~np.isnan(y)] += 40.0
        lab.outcomes._mem[("tp30sl15", 0.0, 0.0)] = y
        for model in ("ridge", "lgbm_reg"):
            lab.evaluate(["launch"], model, 0.0, "tp30sl15", 0.0, 0.2)
        v = lab.verdict(min_trades=40)
        self.assertTrue(v["reality_check"]["p"] < 0.05 and v["qualified"])   # the statistics are satisfied...
        self.assertFalse(v["passes_reality_check"])                        # ...but a zero-cost trial can never be the finding

    def test_the_champion_must_itself_qualify(self):
        lab = fresh_lab()
        lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        v = lab.verdict(min_trades=60)
        self.assertFalse(v["passes_reality_check"])
        self.assertEqual((v["trials"], v["total_trials"], v["stale_trials"]), (1, 1, 0))


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

    def test_changing_the_data_cannot_quietly_reset_the_trial_count(self):
        d = tempfile.mkdtemp()
        ledger = os.path.join(d, "ledger")
        fresh_lab(ledger).evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        # the same recording with one launch removed is a different fingerprint
        rows = read_rows()
        first = next(r["mint"] for r in rows if r.get("k") == "create")
        smaller = os.path.join(d, "smaller.jsonl")
        with open(smaller, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows if r.get("mint") != first)
        lab2 = fresh_lab(ledger, paths=[smaller])
        with self.assertRaises(DataChanged):
            lab2.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        os.environ["RESEARCH_ACK_NEW_DATA"] = "test"
        try:
            lab2.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        finally:
            del os.environ["RESEARCH_ACK_NEW_DATA"]
        v = lab2.verdict()
        self.assertEqual((v["total_trials"], v["trials"], v["stale_trials"]), (2, 1, 1))   # the old trial is still counted
        self.assertTrue(os.path.exists(os.path.join(ledger, "resets.jsonl")))

    def test_a_torn_last_line_and_an_empty_trial_do_not_break_the_ledger(self):
        d = tempfile.mkdtemp()
        lab = fresh_lab(os.path.join(d, "ledger"))
        lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        with open(lab.ledger.path, "a") as f:
            f.write('{"id": "torn", "spec": {"da')   # a crash mid-write
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            e = lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.3)
            self.assertEqual(len(lab.ledger.entries()), 2)
        self.assertIn(e["id"], [x["id"] for x in lab.ledger.entries()])
        with open(lab.ledger.path) as f:
            self.assertTrue(all(line.strip().startswith("{") for line in f if line.strip()))

    def test_the_ledger_is_valid_json_even_when_a_trial_is_empty(self):
        lab = fresh_lab()
        lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
        led = Ledger(tempfile.mkdtemp())
        led.add({"x": 1}, {"mean_per_trade": float("nan"), "n_selected": 0}, np.zeros(3), np.zeros(3, dtype=bool))
        with open(led.path) as fh:
            raw = fh.read()
        self.assertNotIn("NaN", raw)
        self.assertTrue(math.isnan(led.entries()[0]["summary"]["mean_per_trade"]))

    def test_a_budget_needs_an_owner_and_is_enforced(self):
        lab = fresh_lab()
        os.environ["RESEARCH_BUDGET"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
            os.environ["RESEARCH_AGENT"] = "tester"
            lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.2)
            with self.assertRaises(RuntimeError):
                lab.evaluate(["launch"], "ridge", 0.0, "tp30sl15", 0.0, 0.3)
        finally:
            os.environ.pop("RESEARCH_BUDGET", None)
            os.environ.pop("RESEARCH_AGENT", None)


@NEEDS
class SealedConfirmation(unittest.TestCase):
    """Plumbing of the one-shot test on the sealed recording. The 'sealed' file here is a later slice of the dev recording."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        rows = read_rows()
        creates = sorted((r["t"], r["mint"]) for r in rows if r.get("k") == "create")
        cut = creates[int(len(creates) * 0.6)][0]
        gap = 16 * 60_000
        dev_m = {m for t, m in creates if t < cut}
        hold_m = {m for t, m in creates if t >= cut + gap}
        cls.dev, cls.hold = os.path.join(cls.d, "dev.jsonl"), os.path.join(cls.d, "holdout_slice.jsonl")
        with open(cls.dev, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows if r.get("mint") in dev_m)
        with open(cls.hold, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows if r.get("mint") in hold_m)

    def setUp(self):
        from research import confirm
        self.confirm = confirm
        self.work = tempfile.mkdtemp()
        self.saved = confirm.MAX_SPECS, confirm.MIN_TRADES
        confirm.MAX_SPECS, confirm.MIN_TRADES = 2, 30
        self.ledger = os.path.join(self.work, "ledger")
        self.lab = Lab([self.dev], ledger_dir=self.ledger, cache_dir=CACHE)

    def tearDown(self):
        self.confirm.MAX_SPECS, self.confirm.MIN_TRADES = self.saved

    def args(self, name, **kw):
        import argparse
        base = dict(data=self.dev, holdout=self.hold, name=name, features="launch,tape", model="ridge", policy="tp30sl15", delay=0.0, cost=0.0, q=0.3, seed=0, note="t",
                    spec_file=None, ledger_dir=self.ledger, frozen_dir=os.path.join(self.work, "frozen"), log=os.path.join(self.work, "log.jsonl"))
        base.update(kw)
        return argparse.Namespace(**base)

    def trial_and_freeze(self, name, **kw):
        a = self.args(name, **kw)
        self.lab.evaluate(a.features.split(","), a.model, a.delay, a.policy, a.cost, a.q, a.seed)
        a.spec_file = self.confirm.freeze(a)
        return a

    def test_freeze_requires_a_development_trial_then_run_once_and_the_result_is_logged_with_a_decision(self):
        with self.assertRaises(SystemExit):
            self.confirm.freeze(self.args("never_run"))
        a = self.trial_and_freeze("c1")
        r = self.confirm.run(a)
        self.assertGreater(r["n_selected"], 30)
        self.assertEqual(set(r) >= {"mean_per_trade", "ci_lo", "ci_hi", "p_mean_le_zero", "blind_per_trade", "bonferroni_alpha", "passes", "dev_fp", "holdout_fp", "code"}, True)
        self.assertFalse(r["passes"])   # the real data has no edge
        self.assertGreater(r["p_mean_le_zero"], 0.05)
        again = self.confirm.run(a)
        self.assertEqual(again["time"], r["time"])
        self.assertEqual(len(self.confirm.log_entries(a.log)), 1)

    def test_a_frozen_spec_cannot_be_edited_or_overwritten_and_only_a_few_specs_may_use_the_sealed_data(self):
        a = self.trial_and_freeze("c1")
        with self.assertRaises(SystemExit):
            self.confirm.freeze(a)
        self.confirm.run(a)
        for name, q in (("c2", 0.4), ("c3", 0.5)):
            b = self.trial_and_freeze(name, q=q)
            if name == "c2":
                self.confirm.run(b)
            else:
                with self.assertRaises(SystemExit):
                    self.confirm.run(b)

    def test_tampering_with_the_frozen_file_or_the_code_or_the_data_voids_it(self):
        a = self.trial_and_freeze("c1")
        with open(a.spec_file) as f:
            frozen = json.load(f)
        edited = dict(frozen, spec=dict(frozen["spec"], q=0.05))
        with open(a.spec_file, "w") as f:
            json.dump(edited, f)
        with self.assertRaises(SystemExit):
            self.confirm.run(a)
        with open(a.spec_file, "w") as f:
            json.dump(frozen, f)
        saved = self.confirm.code_hash
        self.confirm.code_hash = lambda: "changed"
        try:
            with self.assertRaises(SystemExit):
                self.confirm.run(a)
        finally:
            self.confirm.code_hash = saved
        b = self.args("c1", data=raw_path(), spec_file=a.spec_file)   # a different development recording
        with self.assertRaises(SystemExit):
            self.confirm.run(b)

    def test_a_sealed_recording_that_overlaps_or_precedes_the_development_data_is_refused(self):
        a = self.trial_and_freeze("c1")
        before = self.args("c1", holdout=self.dev.replace("dev.jsonl", "holdout_dev_copy.jsonl"), spec_file=a.spec_file)
        shutil.copy(self.dev, before.holdout)
        with self.assertRaises(SystemExit):
            self.confirm.run(before)
        self.assertTrue(a)

    def test_the_research_loader_refuses_the_sealed_slice_but_confirm_can_open_it(self):
        with self.assertRaises(PermissionError):
            load([self.hold])
        self.assertGreater(len(load([self.hold], unseal=True)), 100)


@NEEDS
class PathsDoNotDependOnTheWorkingDirectory(unittest.TestCase):
    def test_defaults_are_absolute_and_the_same_from_anywhere(self):
        from research import paths
        for p in (paths.LEDGER_DIR, paths.CACHE_DIR, paths.FROZEN_DIR, paths.HOLDOUT_LOG, *paths.DEV_DATA):
            self.assertTrue(os.path.isabs(p), p)
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {ROOT!r}); from research import paths; print(paths.LEDGER_DIR)"],
                             cwd=tempfile.gettempdir(), capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(out.stdout.strip(), paths.LEDGER_DIR)


if __name__ == "__main__":
    unittest.main()
