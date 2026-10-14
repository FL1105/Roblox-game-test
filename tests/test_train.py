import random
import unittest

from sniper.live.analyze import report, token_summary
from sniper.paper.compare import base_strategy, compare, split_by_launch, variants
from sniper.policy.model import HoldModel, Logistic
from sniper.policy.features import FEATURES
from sniper.policy.train import single_feature_aucs, split_tokens, train
from sniper.pump.events import NewToken

from .sim import Sim


def make_path(i, kind, t0):
    """A raw token path (the format live/convert.token_paths returns). kind: 'runner' keeps getting bought, 'flat' does not."""
    sim = Sim(f"M{i:03d}", "C", t0)
    launch = sim.create()
    obs, t = [], t0
    if kind == "runner":
        for step in range(14):
            t += 5_000
            tr = sim.buy(t, 0.8 + 0.1 * step)
            obs.append({"t": tr.t, "v_sol": tr.v_sol, "v_tokens": tr.v_tokens})
    else:
        tr = sim.buy(t0 + 4_000, 0.05)
        obs.append({"t": tr.t, "v_sol": tr.v_sol, "v_tokens": tr.v_tokens})
    end_t = obs[-1]["t"] + 90_000
    return {"create": {"t": t0, "mint": launch.mint, "creator": "C", "dev_tokens": 0.0, "dev_sol": 0.0, "v_sol": launch.v_sol, "v_tokens": launch.v_tokens},
            "obs": obs, "end": {"t": end_t, "reason": "idle"}}


def synthetic_paths(n=80, seed=1):
    rng = random.Random(seed)
    return {f"M{i:03d}": make_path(i, "runner" if rng.random() < 0.4 else "flat", 1_000_000 + i * 200_000) for i in range(n)}


class Pipeline(unittest.TestCase):
    def test_split_is_by_launch_time_and_honours_the_fraction(self):
        paths = synthetic_paths(50)
        tr, te = split_tokens(paths, 0.3)
        self.assertEqual((len(tr), len(te)), (35, 15))
        self.assertLess(max(p["create"]["t"] for p in tr), min(p["create"]["t"] for p in te))

    def test_model_learns_a_planted_pattern_and_beats_noise_on_unseen_launches(self):
        model, text = train(synthetic_paths(80), test_frac=0.3)
        self.assertGreater(model.meta["test_auc_up"], 0.7)
        self.assertIn("test AUC", text)
        self.assertIn("best single feature", text)

    def test_too_little_data_refuses_to_train(self):
        with self.assertRaises(SystemExit):
            train(synthetic_paths(3), test_frac=0.3)

    def test_single_feature_aucs_cover_every_feature(self):
        import numpy as np
        rng = np.random.default_rng(0)
        X = rng.normal(size=(200, len(FEATURES))); y = (X[:, 0] > 0).astype(int)
        aucs = single_feature_aucs(X, y)
        self.assertEqual(set(aucs), set(FEATURES))
        self.assertGreater(aucs[FEATURES[0]], 0.95)


class CompareAndAnalyze(unittest.TestCase):
    def events(self):
        from sniper.live.convert import to_events
        rows = []
        for i, p in enumerate(synthetic_paths(30).values()):
            c = p["create"]
            rows.append({"k": "create", "t": c["t"], "mint": c["mint"], "creator": "C", "symbol": "S", "name": "N", "dev_tokens": 0.0, "dev_sol": 0.0, "v_sol": c["v_sol"], "v_tokens": c["v_tokens"]})
            for j, o in enumerate(p["obs"]):
                rec = {"k": "obs", "t": o["t"], "mint": c["mint"], "v_sol": o["v_sol"], "v_tokens": o["v_tokens"]}
                if j == 0:
                    rec.update(rt_in_sol=0.1, rt_back_sol=0.0969)
                rows.append(rec)
            rows.append({"k": "end", "t": p["end"]["t"], "mint": c["mint"], "reason": "idle"})
        return to_events(rows)

    def test_compare_prints_every_variant_and_adds_the_model_ones_when_given_a_model(self):
        ev = self.events()
        text = compare(ev, None, tx_cost_sol=0.0)
        for name in variants(base_strategy(), False):
            self.assertIn(name, text)
        n = len(FEATURES)
        lg = Logistic([0.0] * n, [1.0] * n, [0.0] * n, -3.0)
        with_model = compare(ev, HoldModel(lg, lg, {}), tx_cost_sol=0.0)
        self.assertIn("learned model", with_model)

    def test_variants_differ_only_in_the_policy_fields(self):
        base = base_strategy()
        v = variants(base, True)
        self.assertEqual(v["confirm + smart exit"].entry_mode, "confirm")
        self.assertEqual(v["confirm + smart exit"].exit_mode, "smart")
        self.assertEqual(v["snipe + learned model"].exit_mode, "model")
        for s in v.values():
            self.assertEqual((s.size_sol, s.max_open, s.tx_cost_sol), (base.size_sol, base.max_open, base.tx_cost_sol))

    def test_holdout_split_keeps_tokens_whole_and_ordered(self):
        ev = self.events()
        tr, te = split_by_launch(ev, 0.6)
        self.assertFalse({e.mint for e in tr} & {e.mint for e in te})
        self.assertLess(max(e.t for e in tr if isinstance(e, NewToken)), min(e.t for e in te if isinstance(e, NewToken)))

    def test_analysis_reports_blind_entry_outcomes(self):
        paths = synthetic_paths(40)
        text = report(paths)
        self.assertIn("BLIND ENTRY", text)
        self.assertIn("first to be hit", text)
        runner = token_summary(make_path(1, "runner", 0))
        flat = token_summary(make_path(2, "flat", 0))
        self.assertGreater(runner["peak"], 30)
        self.assertLess(flat["peak"], 30)


if __name__ == "__main__":
    unittest.main()
